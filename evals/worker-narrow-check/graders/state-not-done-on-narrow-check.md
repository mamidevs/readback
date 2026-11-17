---
type: regex
target: { source: file, path: answer.txt }
pattern: 'state\W{1,40}DONE'
match: not_contains
---
