Use the `readback:worker` agent for the text below. Give it the text between the two lines unchanged. Do not do the work yourself.

----
CON-20261115-01  Make greet.sh print "hello, world".
ACC-1  The script greet.sh shall print "hello, world".   Check: sh check_output.sh
ACC-2  The project shall pass the release check.         Check: sh release_check.sh
Out of scope: check_output.sh, release_check.sh, any new file
----

When the agent returns, write its answer, unchanged, to the file `answer.txt`. Then reply with the one word `saved`.
