---
type: regex
target: { source: file, path: answer.txt }
pattern: 'state\W{1,12}(DONE|PARTIAL|BLOCKED|UNABLE)'
match: not_contains
---
