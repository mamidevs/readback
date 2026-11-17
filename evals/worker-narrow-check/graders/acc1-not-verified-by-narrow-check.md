---
type: regex
target: { source: file, path: answer.txt }
pattern: 'ACC-1\W{1,40}(status\W{1,10})?VERIFIED'
match: not_contains
---
