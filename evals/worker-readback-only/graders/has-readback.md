---
type: regex
target: { source: file, path: answer.txt }
pattern: 'task\W{0,3}:[\s\S]{0,600}limits\W{0,3}:[\s\S]{0,600}done when\W{0,3}:'
flags: i
---
