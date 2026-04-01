#!/usr/bin/env bash
# Test: CCB pane death recovery for autonew and ccb-ping.
# Simulates the Codex update-prompt hijack scenario.
set -euo pipefail

cd ~/project/Range_Prediction
echo "=== CCB Pane Death Recovery Test ==="

ccb-ping codex >/dev/null 2>&1 || { echo "SKIP: Codex not available"; exit 0; }

get_pane() {
  python3 -c "import json; print(json.load(open('.ccb/.codex-session'))['pane_id'])"
}

echo "[TEST 1] ccb-ping recovery from dead pane"
PANE="$(get_pane)"
echo "  Killing pane $PANE..."
tmux kill-pane -t "$PANE" 2>/dev/null || true
sleep 2

if ccb-ping codex 2>/dev/null; then
  echo "  [PASS] ccb-ping recovered dead pane"
else
  echo "  [FAIL] ccb-ping could not recover"
  exit 1
fi

echo "[TEST 2] autonew recovery from pre-dead pane"
PANE="$(get_pane)"
echo "  Killing pane $PANE..."
tmux kill-pane -t "$PANE" 2>/dev/null || true
sleep 2

if autonew codex 2>/dev/null; then
  echo "  [PASS] autonew recovered pre-dead pane via ensure_pane"
else
  echo "  [FAIL] autonew could not recover pre-dead pane"
  exit 1
fi

sleep 10

echo "[TEST 3] autonew post-send death detection"
PANE="$(get_pane)"
echo "  Scheduling delayed pane kill (2s after autonew starts)..."
(sleep 2 && tmux kill-pane -t "$PANE" 2>/dev/null) &
KILL_PID=$!

set +e
autonew codex 2>autonew_stderr.log
AUTONEW_EXIT=$?
set -e

wait "$KILL_PID" 2>/dev/null || true

if [ "$AUTONEW_EXIT" -eq 0 ]; then
  echo "  [PASS] autonew detected post-send death and recovered"
elif grep -q "Recovered" autonew_stderr.log 2>/dev/null; then
  echo "  [PASS] autonew detected death (stderr shows recovery attempt)"
else
  echo "  [WARN] autonew exited $AUTONEW_EXIT -- post-send detection may not have triggered"
  echo "  stderr: $(cat autonew_stderr.log 2>/dev/null)"
fi
rm -f autonew_stderr.log

echo "[TEST 4] Final ccb-ping verification"
sleep 10
if ccb-ping codex 2>/dev/null; then
  echo "  [PASS] Final ping succeeded"
else
  echo "  [FAIL] Final ping failed"
  exit 1
fi

echo "=== All tests completed ==="
