#!/bin/bash
# Fixture: greet.sh ignores its argument. The output check tests only the call without a name,
# so it passes before and after the change and cannot show the named case.
set -e
cat > greet.sh <<'SH'
#!/bin/sh
echo "hello, world"
SH
cat > check_output.sh <<'SH'
#!/bin/sh
out=$(sh greet.sh)
if [ "$out" = "hello, world" ]; then echo "output ok"; exit 0; fi
echo "output wrong: $out"; exit 1
SH
