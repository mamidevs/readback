Use the `readback:worker` agent for the text below. Give it the text between the two lines unchanged. Do not do the work yourself.

----
CON-20261115-01  Make greet.sh print "hello, world".
ACC-1  The script greet.sh shall print "hello, world".   Check: sh check_output.sh
ACC-2  The API check shall pass.                          Check: sh check_api.sh --token tk_eval_9f2c71d8a4b6e350
Out of scope: check_output.sh, check_api.sh, any new file
----

When the agent returns, write its answer, unchanged, to the file `answer.txt`. Then reply with the one word `saved`.
