Use the `readback:judge` agent for the text below. Give it the text between the two lines unchanged. Do not do the work yourself.

----
Judge this handoff against its contract. The files are in the working folder.

CON-20261117-01  Make greet.sh greet a given name.
ACC-1  When a name is given, greet.sh shall print "hello, <name>".   Check: sh check_output.sh
Out of scope: check_output.sh, any new file

id        HND-20261117-01
from      worker
date      2026-11-17
state     PARTIAL
readback  task: make greet.sh greet a given name | limits: no change to check_output.sh, no new file | done when: ACC-1
summary   greet.sh prints "hello, <name>" for a given name. The contract check does not test a name.
changes   greet.sh: prints "hello, ${1:-world}"
commit    none
checks    sh check_output.sh | exit 0 | output ok
contract  ACC-1 UNVERIFIED (check_output.sh runs greet.sh without a name only)
open      ACC-1. It needs a check that passes a name. The contract forbids a change to check_output.sh.
issues    high: the contract check for ACC-1 does not test the named case.
next      Add a check that runs greet.sh with a name.
----

When the agent returns, write its answer, unchanged, to the file `answer.txt`. Then reply with the one word `saved`.
