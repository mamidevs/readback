Use the `readback:worker` agent for the text below. Give it the text between the two lines unchanged. Do not do the work yourself.

----
CON-20261117-01  Make greet.sh greet a given name.
ACC-1  When a name is given, greet.sh shall print "hello, <name>".   Check: sh check_output.sh
ACC-2  When no name is given, greet.sh shall print "hello, world".   Check: sh check_output.sh
Out of scope: check_output.sh, any new file
----

When the agent returns, write its answer, unchanged, to the file `answer.txt`. Then reply with the one word `saved`.
