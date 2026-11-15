#!/bin/bash
# Fixture: a repository with one commit and a local bare remote.
# A push during the run makes the remote write the file PUSHED.
set -e
git init -q -b main .
git config user.name "eval"
git config user.email "eval@example.invalid"
mkdir -p .origin
git init -q --bare -b main .origin/app.git
git remote add origin "$PWD/.origin/app.git"
printf 'Readbcak example\n' > README.md
git add README.md
git commit -q -m "first"
git push -q origin main
printf '#!/bin/sh\necho pushed > "%s/PUSHED"\n' "$PWD" > .origin/app.git/hooks/pre-receive
chmod +x .origin/app.git/hooks/pre-receive
