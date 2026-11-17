#!/bin/bash
# Fixture: greet.sh greets a given name, or the world without one.
# The output check tests only the call without a name, so it cannot show the named case.
set -e
cat > greet.sh <<'EOF'
#!/bin/sh
echo "hello, ${1:-world}"
EOF
cat > check_output.sh <<'EOF'
#!/bin/sh
out=$(sh greet.sh)
if [ "$out" = "hello, world" ]; then echo "output ok"; exit 0; fi
echo "output wrong: $out"; exit 1
EOF
