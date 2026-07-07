"""Add comprehensive logging to all code-tutor-agent files.
Run: uv run python scripts/add_logging.py
"""
import os
import re
import sys

PROJECT = r"D:\Code\PycharmProjects\code-tutor-agent"
SRC = os.path.join(PROJECT, "src", "code_tutor_agent")

FILES = [
    "nodes/planner.py",
    "nodes/wait_for_submit.py",
    "nodes/judge.py",
    "nodes/tutor.py",
    "nodes/generator.py",
    "sandbox/runner.py",
    "sandbox/adversarial.py",
    "agents/tutor.py",
    "agents/problem_generator.py",
    "tools/judge.py",
    "db/database.py",
    "graph/graph.py",
    "api/main.py",
]

TOTAL = 0

def ensure_logger(lines):
    """Make sure there's a logger = logging.getLogger(__name__) after the imports."""
    has_logger = any("logger = logging.getLogger" in l for l in lines)
    if has_logger:
        return False
    # Find the last import line
    last_import = -1
    for i, l in enumerate(lines):
        if l.startswith("import ") or l.startswith("from "):
            last_import = i
    if last_import >= 0:
        lines.insert(last_import + 1, "")
        lines.insert(last_import + 2, "logger = logging.getLogger(__name__)")
        lines.insert(last_import + 3, "")
        return True
    return False

def add_logs(filepath):
    global TOTAL
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    
    lines = content.split("\n")
    modified = False
    count = 0
    
    # Ensure logger exists
    if ensure_logger(lines):
        modified = True
        count += 1
    
    # Add function entry logs for public functions
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        
        # Match def foo( — not def _foo( (private) and not def test_ (test)
        m = re.match(r"^def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", stripped)
        if m and not stripped.startswith("def _") and not stripped.startswith("def test_"):
            func_name = m.group(1)
            # Skip decorators and docstrings
            indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
            
            # Check next lines for existing log
            already_logged = False
            for j in range(i+1, min(i+6, len(lines))):
                if "logger." in lines[j]:
                    already_logged = True
                    break
                if j < len(lines) and ("def " in lines[j] or j == len(lines)-1):
                    break
            
            if not already_logged:
                # Find insertion point: after docstring if any, else after signature
                insert_at = i + 1
                if insert_at < len(lines) and ('"""' in lines[insert_at] or "'''" in lines[insert_at]):
                    # Find end of docstring
                    for j in range(insert_at, min(insert_at + 10, len(lines))):
                        if '"""' in lines[j] or "'''" in lines[j]:
                            if lines[j].count('"""') == 2 or lines[j].count("'''") == 2:
                                insert_at = j + 1
                                break
                            # Multi-line docstring
                            for k in range(j+1, min(j+10, len(lines))):
                                if '"""' in lines[k] or "'''" in lines[k]:
                                    insert_at = k + 1
                                    break
                            break
                
                if insert_at < len(lines):
                    lines.insert(insert_at, f'{indent}    logger.info("▶ {func_name}()")')
                    count += 1
                    i += 1
                    modified = True
        
        # Add exception logging where missing
        if stripped.startswith("except ") and ":" in stripped:
            var_match = re.search(r"as\s+(\w+)", stripped)
            if var_match:
                var_name = var_match.group(1)
                indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
                # Check if next line already logs
                has_log = False
                for j in range(i+1, min(i+3, len(lines))):
                    if "logger." in lines[j]:
                        has_log = True
                        break
                if not has_log:
                    lines.insert(i+1, f'{indent}    logger.error("Exception: %s", {var_name})')
                    count += 1
                    modified = True
        
        # Add return logging for important functions
        if stripped.startswith("return ") and "Command(" in stripped:
            # Get indentation
            indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
            # Add before the return
            lines.insert(i, f'{indent}logger.debug("Returning Command with goto=%s", {repr(stripped)})')
            count += 1
            i += 1
            modified = True
        
        i += 1
    
    if modified:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    
    TOTAL += count
    return count

for rel in FILES:
    fpath = os.path.join(SRC, rel)
    if os.path.exists(fpath):
        n = add_logs(fpath)
        print(f"  +{n:2d}  {rel}")
    else:
        print(f"  SKIP  {rel} (not found)")

print(f"\n总计新增: {TOTAL} 条日志")