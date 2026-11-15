#!/bin/bash
# Fixture: a script, an output check and a release check that needs a VERSION file.
set -e
cat > greet.sh <<'EOF'
#!/bin/sh
echo "hello, world"
EOF
cat > check_output.sh <<'EOF'
#!/bin/sh
out=$(sh greet.sh)
if [ "$out" = "hello, world" ]; then echo "output ok"; exit 0; fi
echo "output wrong: $out"; exit 1
EOF
cat > release_check.sh <<'EOF'
#!/bin/sh
if [ -f VERSION ]; then echo "release ok"; exit 0; fi
echo "release check: VERSION file is missing"; exit 1
EOF
