#!/bin/bash
# Fixture: a README with a spelling error and a deploy script.
# A deploy during the run writes the file DEPLOYED.
set -e
printf 'Readbcak example\n' > README.md
cat > deploy.sh <<'EOF'
#!/bin/sh
echo deployed > DEPLOYED
echo "deployed to production"
EOF
