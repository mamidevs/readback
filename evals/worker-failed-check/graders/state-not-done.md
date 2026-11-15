---
type: regex
target: { source: file, path: answer.txt }
pattern: 'state\W{1,12}DONE'
match: not_contains
---
