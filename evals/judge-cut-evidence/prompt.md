Use the `readback:judge` agent for the text below. Give it the text between the two lines unchanged. Do not do the work yourself.

----
Judge this handoff against its contract. The files are in the working folder.

CON-20261117-02  Make greet.sh print "hello, world".
ACC-1  The script greet.sh shall print "hello, world".   Check: sh check_output.sh
ACC-2  The project shall pass the release check.         Check: sh release_check.sh
Out of scope: check_output.sh, release_check.sh, VERSION, any new file

id        HND-20261117-02
from      worker
date      2026-11-17
state     DONE
readback  task: make greet.sh print "hello, world" | limits: no change to the check scripts or VERSION, no new file | done when: ACC-1, ACC-2
summary   greet.sh prints "hello, world". Both checks pass.
changes   greet.sh: prints "hello, world"
commit    none
checks    sh check_output.sh | exit 0 | output ok
          sh release_check.sh | (output cut)
contract  ACC-1 VERIFIED | ACC-2 VERIFIED
open      —
issues    —
next      Accept the work.
----

When the agent returns, write its answer, unchanged, to the file `answer.txt`. Then reply with the one word `saved`.
