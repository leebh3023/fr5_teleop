#!/usr/bin/env bash
set -Eeuo pipefail

HOTFIX_ID="rc2-hf1"
MODE="apply"
ROOT_DIR="${PWD}"
ALLOW_RUNNING="false"

usage() {
  printf '%s\n' \
    "사용법: $(basename "$0") [--check] [--allow-running] [RC2_ROOT]" \
    "" \
    "  --check          파일을 쓰지 않고 예상 변경 라인만 표시" \
    "  --allow-running  실행 중인 teleop 프로세스가 있어도 파일만 수정" \
    "  RC2_ROOT         teleop/ 과 config.yaml 이 있는 rc2 폴더 (기본: 현재 폴더)"
}

while (($#)); do
  case "$1" in
    --check)
      MODE="check"
      ;;
    --allow-running)
      ALLOW_RUNNING="true"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      printf '[오류] 알 수 없는 옵션: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      ROOT_DIR="$1"
      ;;
  esac
  shift
done

if [[ "$MODE" == "apply" && "$ALLOW_RUNNING" != "true" ]] && command -v pgrep >/dev/null 2>&1; then
  RUNNING_PROCESSES="$(
    pgrep -af '(^|/)(python|python3)([[:space:]].*)?[[:space:]](-m[[:space:]]+teleop|[^[:space:]]*server\.py)' \
      || true
  )"
  if [[ -n "$RUNNING_PROCESSES" ]]; then
    printf '%s\n' \
      "[중단] 실행 중인 teleop 프로세스가 감지됐습니다." \
      "$RUNNING_PROCESSES" \
      "" \
      "그립을 놓고 로봇 정지를 확인한 뒤 서비스를 정상 종료하고 다시 실행하세요." \
      "이 스크립트는 로봇이나 서비스를 자동으로 정지·재시작하지 않습니다." >&2
    exit 3
  fi
fi

PYTHON_BIN=""
for candidate in \
  "$ROOT_DIR/.venv/bin/python" \
  "$ROOT_DIR/.venv-linux/bin/python" \
  "$(command -v python3 2>/dev/null || true)"
do
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  printf '[오류] Python 3 실행 파일을 찾지 못했습니다.\n' >&2
  exit 4
fi

"$PYTHON_BIN" - "$ROOT_DIR" "$MODE" "$HOTFIX_ID" <<'PY'
from __future__ import annotations

import difflib
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time


root = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
hotfix_id = sys.argv[3]


def fail(message: str) -> None:
    print(f"[오류] {message}", file=sys.stderr)
    raise SystemExit(10)


def find_adapter(project_root: Path) -> Path:
    direct = project_root / "teleop" / "robot" / "fairino_client.py"
    if direct.is_file():
        return direct

    matches = [
        path
        for path in project_root.glob("*/teleop/robot/fairino_client.py")
        if "wheelhouse" not in path.parts and not any(".bak" in part for part in path.parts)
    ]
    if len(matches) != 1:
        fail(
            "teleop/robot/fairino_client.py를 하나만 찾을 수 있어야 합니다. "
            f"발견 개수={len(matches)}"
        )
    return matches[0]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        fail(f"{path} 읽기 실패: {exc}")


def setting_value(text: str, name: str) -> tuple[re.Match[str], float]:
    pattern = re.compile(
        rf"(?m)^(?P<prefix>\s*{re.escape(name)}\s*:\s*)"
        r"(?P<value>[0-9]+(?:\.[0-9]+)?)"
        r"(?P<suffix>\s*(?:#.*)?)$"
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        fail(f"config.yaml에서 {name} 설정을 하나만 찾아야 합니다. 발견 개수={len(matches)}")
    return matches[0], float(matches[0].group("value"))


def replace_setting(
    text: str,
    name: str,
    *,
    target: float,
    expected: set[float],
    rendered_target: str,
) -> str:
    match, current = setting_value(text, name)
    if abs(current - target) < 1e-12:
        return text
    if not any(abs(current - value) < 1e-12 for value in expected):
        fail(
            f"{name}={current}는 rc2 예상값이 아닙니다. "
            "현장 사용자 설정을 자동으로 덮어쓰지 않았습니다."
        )
    replacement = (
        f"{match.group('prefix')}{rendered_target}{match.group('suffix')}"
    )
    return text[: match.start()] + replacement + text[match.end() :]


def changed_line_counts(before: str, after: str) -> tuple[int, int, int]:
    matcher = difflib.SequenceMatcher(
        a=before.splitlines(),
        b=after.splitlines(),
        autojunk=False,
    )
    logical = 0
    added = 0
    deleted = 0
    for tag, first_start, first_end, second_start, second_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_count = first_end - first_start
        new_count = second_end - second_start
        logical += max(old_count, new_count)
        deleted += old_count
        added += new_count
    return logical, added, deleted


adapter_path = find_adapter(root)
project_root = adapter_path.parents[2]
config_path = project_root / "config.yaml"
version_path = project_root / "teleop" / "__init__.py"
worker_path = project_root / "teleop" / "robot" / "worker.py"
adapter_test_path = project_root / "tests" / "unit" / "test_fairino_adapter.py"

if not config_path.is_file():
    fail(f"config.yaml을 찾지 못했습니다: {config_path}")
if not worker_path.is_file():
    fail(f"worker.py를 찾지 못했습니다: {worker_path}")
if version_path.is_file() and "0.2.0rc2" not in read_text(version_path):
    fail(f"{project_root}는 vr-teleop 0.2.0rc2가 아닙니다.")

adapter_before = read_text(adapter_path)
adapter_after = adapter_before

unsafe_servo_block = """\
        if self._servo_cart_supports_exaxis is not False:
            try:
                result = self._robot.ServoCart(
                    **arguments,
                    exaxis=list(self.config.exaxis_default),
                )
                self._servo_cart_supports_exaxis = True
            except TypeError as exc:
                if "unexpected keyword argument 'exaxis'" not in str(exc):
                    raise RobotClientError(
                        "ServoCart", None, f"SDK signature error: {exc}"
                    ) from exc
                self._servo_cart_supports_exaxis = False
                log.warning(
                    "Fairino SDK ServoCart has no exaxis parameter; "
                    "using legacy signature"
                )
                result = self._robot.ServoCart(**arguments)
        else:
            result = self._robot.ServoCart(**arguments)
"""

safe_servo_block = """\
        if self._servo_cart_supports_exaxis is None:
            raise RobotClientError(
                "ServoCart",
                None,
                "ServoCart signature was not verified before motion",
            )
        if self._servo_cart_supports_exaxis:
            arguments["exaxis"] = list(self.config.exaxis_default)
        result = self._robot.ServoCart(**arguments)
"""

signature_probe = """\
        self._robot = self._robot_module.RPC(self.config.robot_ip)
        # RC2-HF1: motion RPC를 시험 호출하지 않고 로컬 Python signature만 검사한다.
        try:
            servo_cart_signature = inspect.signature(self._robot.ServoCart)
        except (TypeError, ValueError) as exc:
            raise RobotClientError(
                "connect",
                None,
                "cannot inspect ServoCart signature safely; refusing hardware mode",
            ) from exc
        servo_cart_parameters = servo_cart_signature.parameters
        if any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in servo_cart_parameters.values()
        ):
            raise RobotClientError(
                "connect",
                None,
                "ambiguous ServoCart **kwargs signature; refusing hardware mode",
            )
        self._servo_cart_supports_exaxis = "exaxis" in servo_cart_parameters
        log.info(
            "ServoCart signature selected locally: exaxis=%s signature=%s",
            self._servo_cart_supports_exaxis,
            servo_cart_signature,
        )
"""

if unsafe_servo_block in adapter_after:
    if "import inspect\n" not in adapter_after:
        import_anchor = "import importlib\n"
        if adapter_after.count(import_anchor) != 1:
            fail("fairino_client.py의 import 위치가 rc2 예상 구조와 다릅니다.")
        adapter_after = adapter_after.replace(
            import_anchor,
            import_anchor + "import inspect\n",
            1,
        )

    rpc_anchor = "        self._robot = self._robot_module.RPC(self.config.robot_ip)\n"
    if "RC2-HF1:" not in adapter_after:
        if adapter_after.count(rpc_anchor) != 1:
            fail("fairino_client.py의 RPC 생성 위치가 rc2 예상 구조와 다릅니다.")
        adapter_after = adapter_after.replace(rpc_anchor, signature_probe, 1)

    adapter_after = adapter_after.replace(
        unsafe_servo_block,
        safe_servo_block,
        1,
    )
elif (
    "inspect.signature(self._robot.ServoCart)" in adapter_after
    and "unexpected keyword argument 'exaxis'" not in adapter_after
):
    print("[정보] ServoCart 로컬 사전검사가 이미 적용되어 adapter 변경을 생략합니다.")
else:
    fail(
        "fairino_client.py가 알려진 rc2 또는 현장 사전검사 버전과 다릅니다. "
        "오인 패치를 막기 위해 중단했습니다."
    )

try:
    compile(adapter_after, str(adapter_path), "exec")
except SyntaxError as exc:
    fail(f"수정 예정 adapter 문법 검증 실패: {exc}")

worker_before = read_text(worker_path)
worker_after = worker_before
timeout_block = """\
            elif state == WorkerState.ACTIVE:
                if not fresh:
                    stop_servo(now_ns, "pose_timeout")
"""
diagnostic_timeout_block = """\
            elif state == WorkerState.ACTIVE:
                if not fresh:
                    # RC2-HF1: INFO/DEBUG 전환 없이 현장 timeout 근거를 남긴다.
                    pose_age_ms = (
                        max(0.0, (now_ns - pose.received_ns) / 1_000_000)
                        if pose.valid and pose.received_ns
                        else None
                    )
                    log.warning(
                        "pose timeout stopping servo: generation=%d pose_seq=%s "
                        "input_age_ms=%s timeout_ms=%.1f "
                        "last_sdk_call_ms=%.3f missed_ticks=%d",
                        spec.generation,
                        pose.seq if pose.valid else None,
                        f"{pose_age_ms:.1f}" if pose_age_ms is not None else None,
                        config.pose_timeout_s * 1000,
                        last_sdk_call_ms,
                        counters.missed_ticks,
                    )
                    stop_servo(now_ns, "pose_timeout")
"""
if timeout_block in worker_after:
    worker_after = worker_after.replace(
        timeout_block,
        diagnostic_timeout_block,
        1,
    )
elif "pose timeout stopping servo:" not in worker_after:
    fail("worker.py의 pose timeout 분기가 rc2 예상 구조와 다릅니다.")

try:
    compile(worker_after, str(worker_path), "exec")
except SyntaxError as exc:
    fail(f"수정 예정 worker 문법 검증 실패: {exc}")

test_change: tuple[Path, str, str] | None = None
if adapter_test_path.is_file():
    test_before = read_text(adapter_test_path)
    test_after = test_before

    old_stub = """\
    def ServoCart(self, **kwargs):
        self.servo_cart_call = kwargs
        return 0
"""
    new_stub = """\
    def ServoCart(
        self,
        mode,
        desc_pos,
        exaxis,
        pos_gain,
        acc,
        vel,
        cmdT,
        filterT,
        gain,
    ):
        self.servo_cart_call = {
            "mode": mode,
            "desc_pos": desc_pos,
            "exaxis": exaxis,
            "pos_gain": pos_gain,
            "acc": acc,
            "vel": vel,
            "cmdT": cmdT,
            "filterT": filterT,
            "gain": gain,
        }
        return 0
"""
    if old_stub in test_after:
        test_after = test_after.replace(old_stub, new_stub, 1)
    elif new_stub not in test_after:
        fail("adapter test의 modern ServoCart stub이 rc2 예상 구조와 다릅니다.")

    helper_anchor = """\
    client._robot = stub
    return client, stub
"""
    helper_replacement = """\
    client._robot = stub
    client._servo_cart_supports_exaxis = True
    return client, stub
"""
    if helper_anchor in test_after:
        test_after = test_after.replace(helper_anchor, helper_replacement, 1)
    elif helper_replacement not in test_after:
        fail("adapter test helper가 rc2 예상 구조와 다릅니다.")

    legacy_anchor = """\
    legacy = LegacyStubRobot()
    client._robot = legacy
    target = (1.0, 2.0, 3.0, 180.0, 0.0, 0.0)
"""
    legacy_replacement = """\
    legacy = LegacyStubRobot()
    client._robot = legacy
    client._servo_cart_supports_exaxis = False
    target = (1.0, 2.0, 3.0, 180.0, 0.0, 0.0)
"""
    if legacy_anchor in test_after:
        test_after = test_after.replace(legacy_anchor, legacy_replacement, 1)
    elif legacy_replacement not in test_after:
        fail("adapter legacy test가 rc2 예상 구조와 다릅니다.")

    try:
        compile(test_after, str(adapter_test_path), "exec")
    except SyntaxError as exc:
        fail(f"수정 예정 adapter test 문법 검증 실패: {exc}")
    test_change = (adapter_test_path, test_before, test_after)

config_before = read_text(config_path)
config_after = replace_setting(
    config_before,
    "pose_timeout_s",
    target=0.200,
    expected={0.100, 0.500},
    rendered_target="0.200",
)
config_after = replace_setting(
    config_after,
    "max_step_mm",
    target=0.75,
    expected={1.5},
    rendered_target="0.75",
)
_, watchdog_value = setting_value(config_after, "worker_watchdog_s")
if watchdog_value <= 0.200:
    fail(
        "worker_watchdog_s는 hotfix pose_timeout_s(0.200)보다 커야 합니다. "
        "watchdog을 자동 완화하지 않았습니다."
    )

planned = [
    (adapter_path, adapter_before, adapter_after),
    (worker_path, worker_before, worker_after),
    (config_path, config_before, config_after),
]
if test_change is not None:
    planned.append(test_change)
changed = [item for item in planned if item[1] != item[2]]

print(f"[대상] {project_root}")
print(f"[모드] {'검사만' if mode == 'check' else '적용'}")

total_logical = 0
total_added = 0
total_deleted = 0
for path, before, after in planned:
    logical, added, deleted = changed_line_counts(before, after)
    total_logical += logical
    total_added += added
    total_deleted += deleted
    label = "변경 없음" if logical == 0 else f"논리 {logical}줄 (+{added}/-{deleted})"
    print(f"[파일] {path.relative_to(project_root)}: {label}")

if mode == "check":
    print(
        f"[예상 합계] 논리 변경 {total_logical}줄 "
        f"(추가 {total_added}줄 / 삭제 {total_deleted}줄)"
    )
    raise SystemExit(0)

if not changed:
    print(f"[완료] {hotfix_id}가 이미 적용되어 수정된 라인은 0줄입니다.")
    raise SystemExit(0)

stamp = time.strftime("%Y%m%d-%H%M%S")
backups: list[Path] = []
for path, before, after in changed:
    backup = path.with_name(f"{path.name}.pre-{hotfix_id}-{stamp}.bak")
    shutil.copy2(path, backup)
    backups.append(backup)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.{hotfix_id}.",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(after)
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()

for backup in backups:
    print(f"[백업] {backup.relative_to(project_root)}")
print(
    f"[완료] 논리 변경 {total_logical}줄 "
    f"(추가 {total_added}줄 / 삭제 {total_deleted}줄)"
)
print(
    "[설정] pose_timeout_s=0.200, max_step_mm=0.75, "
    f"worker_watchdog_s={watchdog_value:g}"
)
print("[중요] 실행 중인 프로세스에는 반영되지 않습니다. 이 스크립트는 서비스를 재시작하지 않습니다.")
PY
