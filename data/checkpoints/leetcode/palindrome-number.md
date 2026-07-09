# Palindrome Number

**难度**: Easy  
**标签**: Math

---

## 题目描述

Given an integer x , return true if x is a palindrome , and false otherwise . Example 1: Example 2: Example 3: Constraints: -2 31 <= x <= 2 31 - 1 Follow up: Could you solve it without converting the integer to a string?

## 示例

### 示例 1

```
Input: x = 121 Output: true Explanation: 121 reads as 121 from left to right and from right to left.
```

### 示例 2

```
Input: x = -121 Output: false Explanation: From left to right, it reads -121. From right to left, it becomes 121-. Therefore it is not a palindrome.
```

### 示例 3

```
Input: x = 10 Output: false Explanation: Reads 01 from right to left. Therefore it is not a palindrome.
```

## 约束条件

- -2 31 <= x <= 2 31 - 1

## 进阶

Could you solve it without converting the integer to a string?

## 提示

<details><summary>提示 1</summary>

Beware of overflow when you reverse the integer.

</details>

---

## 模板代码

```python
class Solution:
    def isPalindrome(self, x: int) -> bool:
        
```
