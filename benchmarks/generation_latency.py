"""Benchmark: two problem-generation strategies compared end-to-end.

Strategy A — Current (two-phase):
    1. LLM generates problem + brute_solution (first LLM call)
    2. Generator node parses examples → runs brute on 2 samples → returns
    3. User sees problem immediately (fast TTFT)
    4. While user writes code, background thread generates full test suite:
       - 12 random inputs → brute force → expected outputs
       - LLM generates boundary cases (second LLM call)
       - Brute force on boundaries → expected outputs
       - Save to DB
    Total time = TTFT + bg_time

Strategy B — Single-call (one-phase):
    1. LLM generates problem + brute_solution + examples (single LLM call)
    2. Parse + validate examples → done
    3. No background test generation needed
    Total time = TTFT (same as total)

Strategy B-enhanced — Single-call with more examples:
    Same as B but prompt asks for 4-5 examples instead of 2-3.
    More built-in test cases = less reliance on background gen.

We measure:
    - TTFT (time-to-first-interaction): what user actually waits
    - Total time to have full test suite ready
    - Number of test cases available at TTFT
    - Success rate
    - LLM call count

Run with: python benchmarks/generation_latency.py
"""

from __future__ import annotations

import logging
import statistics
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ──
NUM_RUNS = 5  # how many times to run each strategy per topic
TOPICS = [
    ("数组", "medium"),
    ("双指针", "easy"),
    ("动态规划", "medium"),
]

SRC_ROOT = Path(__file__).parent.parent / "code_tutor_agent"
import sys
sys.path.insert(0, str(SRC_ROOT.parent))

from code_tutor_agent.agents.problem_generator import generate_problem
from code_tutor_agent.models.problem import Problem
from code_tutor_agent.sandbox.runner import run_solution
from code_tutor_agent.leetcode.leetcode_fetcher import _parse_examples_to_test_cases


# ====================================================================
# Shared helpers
# ====================================================================

def _llm_call(topic: str, difficulty: str) -> tuple[Problem | None, float, int]:
    """Call LLM to generate a problem. Returns (problem, elapsed_s, attempts)."""
    t0 = time.perf_counter()
    attempts = 0
    problem = None
    for attempt in range(1, 4):
        attempts = attempt
        try:
            problem = generate_problem(topic=topic, difficulty=difficulty, max_retries=0)
            # Verify brute compiles
            brute = problem.brute_solution or ""
            if not brute:
                logger.warning("No brute_solution, retrying")
                problem = None
                continue
            compile(brute, "<brute>", "exec")
            break
        except Exception as e:
            logger.warning("LLM attempt %d failed: %s", attempt, e)
            problem = None
            continue
    elapsed = time.perf_counter() - t0
    return problem, elapsed, attempts


def _parse_and_validate(problem: Problem) -> tuple[list[dict], float]:
    """Parse examples into test cases and fill expected_output via brute force.
    Returns (test_cases, elapsed_s).
    """
    t0 = time.perf_counter()
    examples = problem.examples or []
    func_sig = problem.function_signature or ""
    sample_tcs = _parse_examples_to_test_cases(examples, func_sig)

    brute = problem.brute_solution or ""
    for tc in sample_tcs:
        results = run_solution(brute, [tc], timeout=10.0)
        if results and results[0].detail:
            tc["expected_output"] = results[0].detail
    elapsed = time.perf_counter() - t0
    return sample_tcs, elapsed


# ====================================================================
# Strategy A: Two-phase (current)
# ====================================================================

def run_strategy_a(topic: str, difficulty: str) -> dict:
    """Phase 1: LLM generates problem + brute only.
    Phase 2 (background): simulate full test suite generation.
    """
    timings = {}

    # Phase 1
    problem, llm_s, attempts = _llm_call(topic, difficulty)
    timings["llm_call_s"] = llm_s

    if problem is None:
        return {"success": False, "timings": timings, "attempts": attempts, "num_examples": 0}

    # Parse examples
    sample_tcs, parse_s = _parse_and_validate(problem)
    timings["parse_and_validate_s"] = parse_s

    # Phase 2: measure background test generation (not part of user wait)
    timings["phase2_bg_s"] = _measure_phase2(problem, difficulty)

    return {
        "success": True,
        "timings": timings,
        "attempts": attempts,
        "num_examples": len(sample_tcs),
    }


def _measure_phase2(problem: Problem, difficulty: str) -> float:
    """Measure background test generation time."""
    t0 = time.perf_counter()
    brute = problem.brute_solution or ""
    func_sig = problem.function_signature or ""

    from code_tutor_agent.sandbox.input_generator import generate_random_inputs

    random_inputs = generate_random_inputs(func_sig, count=12, seed=int(time.time() * 1000))
    if not random_inputs:
        return time.perf_counter() - t0

    # Run brute on all random inputs
    for inp in random_inputs:
        tc = {"input_args": inp, "expected_output": "", "is_hidden": True}
        results = run_solution(brute, [tc], timeout=10.0)
        if results and results[0].detail:
            tc["expected_output"] = results[0].detail

    # LLM boundary generation
    try:
        from code_tutor_agent.config import get_llm
        from code_tutor_agent.prompts.generate_boundary_cases import (
            GENERATE_BOUNDARY_SYSTEM,
            GENERATE_BOUNDARY_USER,
        )
        constraints_str = "\n".join(problem.constraints or [])
        prompt_user = GENERATE_BOUNDARY_USER.format(
            title=problem.title,
            description=problem.description,
            difficulty=difficulty,
            function_signature=func_sig,
            constraints=constraints_str,
            brute_code=brute,
            existing_cases="",
            count=8,
        )
        llm = get_llm("sensenova", temperature=0.5)
        llm.invoke([
            ("system", GENERATE_BOUNDARY_SYSTEM),
            ("human", prompt_user),
        ])
    except Exception:
        pass

    return time.perf_counter() - t0


# ====================================================================
# Strategy B: Single-call (one-phase)
# ====================================================================

def run_strategy_b(topic: str, difficulty: str) -> dict:
    """Single LLM call generates problem + brute + examples.
    No background phase needed.
    """
    timings = {}

    problem, llm_s, attempts = _llm_call(topic, difficulty)
    timings["llm_call_s"] = llm_s

    if problem is None:
        return {"success": False, "timings": timings, "attempts": attempts, "num_examples": 0}

    sample_tcs, parse_s = _parse_and_validate(problem)
    timings["parse_and_validate_s"] = parse_s

    return {
        "success": True,
        "timings": timings,
        "attempts": attempts,
        "num_examples": len(sample_tcs),
    }


# ====================================================================
# Strategy B-enhanced: Single call with more examples
# ====================================================================

def run_strategy_b_enhanced(topic: str, difficulty: str) -> dict:
    """Same as B but we inject a prompt modifier to ask for more examples."""
    timings = {}

    # Temporarily patch the user prompt to ask for more examples
    from code_tutor_agent.prompts import generate_problem as gp_module
    orig_user = gp_module.GENERATE_PROBLEM_USER

    try:
        gp_module.GENERATE_PROBLEM_USER = (
            orig_user + "\n\n要求：请生成至少 4 个示例测试用例，覆盖正常情况和边界情况。"
        )
        problem, llm_s, attempts = _llm_call(topic, difficulty)
    finally:
        gp_module.GENERATE_PROBLEM_USER = orig_user

    timings["llm_call_s"] = llm_s

    if problem is None:
        return {"success": False, "timings": timings, "attempts": attempts, "num_examples": 0}

    sample_tcs, parse_s = _parse_and_validate(problem)
    timings["parse_and_validate_s"] = parse_s

    return {
        "success": True,
        "timings": timings,
        "attempts": attempts,
        "num_examples": len(sample_tcs),
    }


# ====================================================================
# Main benchmark
# ====================================================================

def run_benchmark():
    all_results: dict[str, list[dict]] = {
        "A": [],
        "B": [],
        "B-enhanced": [],
    }

    for topic, diff in TOPICS:
        logger.info("=" * 60)
        logger.info("Topic: %s (%s)", topic, diff)
        logger.info("=" * 60)

        for label, fn in [("A", run_strategy_a), ("B", run_strategy_b), ("B-enhanced", run_strategy_b_enhanced)]:
            logger.info("  Running Strategy %s...", label)
            for i in range(NUM_RUNS):
                r = fn(topic, diff)
                r["topic"] = topic
                r["difficulty"] = diff
                r["run"] = i + 1
                all_results[label].append(r)
                status = "OK" if r["success"] else "FAIL"
                llm_t = r["timings"].get("llm_call_s", 0)
                logger.info("    %s #%d %s/%s: %s (%.1fs LLM)", label, i+1, topic, diff, status, llm_t)

    # ── Per-strategy summaries ──
    for label in ["A", "B", "B-enhanced"]:
        _print_summary(label, all_results[label])

    # ── Cross-topic comparison ──
    logger.info("\n\n" + "=" * 70)
    logger.info("DETAILED COMPARISON BY TOPIC")
    logger.info("=" * 70)

    for topic, diff in TOPICS:
        for label in ["A", "B", "B-enhanced"]:
            subset = [r for r in all_results[label] if r["topic"] == topic and r["success"]]
            if not subset:
                continue
            llm_times = [r["timings"]["llm_call_s"] for r in subset]
            parse_times = [r["timings"]["parse_and_validate_s"] for r in subset]
            examples = [r["num_examples"] for r in subset]

            logger.info("\n  [%s %s] — Strategy %s (%d/%d succeeded)", topic, diff, label, len(subset), NUM_RUNS)
            logger.info("    LLM:     avg=%.1fs  median=%.1fs  min=%.1fs  max=%.1fs",
                        statistics.mean(llm_times), statistics.median(llm_times), min(llm_times), max(llm_times))
            logger.info("    Parse:   avg=%.1fs", statistics.mean(parse_times))
            logger.info("    Examples: avg=%.1f", statistics.mean(examples))

            if label == "A":
                bg_times = [r["timings"]["phase2_bg_s"] for r in subset]
                if any(bg_times):
                    logger.info("    Phase2:  avg=%.1fs  median=%.1fs",
                                statistics.mean(bg_times), statistics.median(bg_times))

            # TTFT = user wait time (LLM + parse for A/B/B-enhanced)
            ttft = statistics.mean(llm_times) + statistics.mean(parse_times)
            logger.info("    TTFT:    %.1fs (user waits this long)", ttft)

            # Total = TTFT + bg for A
            if label == "A":
                total = ttft + (statistics.mean(bg_times) if any(bg_times) else 0)
                logger.info("    Total:   %.1fs (full suite ready)", total)
            else:
                logger.info("    Total:   %.1fs (full suite ready = TTFT, no bg gen needed)", ttft)


def _print_summary(label: str, results: list[dict]) -> None:
    """Print overall summary for a strategy."""
    success = [r for r in results if r["success"]]
    failures = len(results) - len(success)

    logger.info("")
    logger.info("Strategy %s: %d/%d succeeded (%d failed)", label, len(success), len(results), failures)

    if not success:
        return

    llm_times = [r["timings"]["llm_call_s"] for r in success]
    parse_times = [r["timings"]["parse_and_validate_s"] for r in success]
    examples = [r["num_examples"] for r in success]

    logger.info("  LLM call:     avg=%.1fs  median=%.1fs  min=%.1fs  max=%.1fs",
                statistics.mean(llm_times), statistics.median(llm_times), min(llm_times), max(llm_times))
    logger.info("  Parse+valid:  avg=%.1fs  median=%.1fs",
                statistics.mean(parse_times), statistics.median(parse_times))
    logger.info("  Examples:     avg=%.1f  min=%d  max=%d",
                statistics.mean(examples), min(examples), max(examples))

    if label == "A":
        bg_times = [r["timings"].get("phase2_bg_s", 0) for r in success if "phase2_bg_s" in r["timings"]]
        if bg_times:
            logger.info("  Phase2 (bg):  avg=%.1fs  median=%.1fs",
                        statistics.mean(bg_times), statistics.median(bg_times))


if __name__ == "__main__":
    run_benchmark()
