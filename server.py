#!/usr/bin/env python3
"""
VR Teleop Bridge Server for Fairino FR5
========================================
Quest 3 WebXR 컨트롤러 → WebSocket → ServoCart → FR5 로봇

사용법:
    python server.py                    # 드라이런 (로봇 미연결)
    python server.py --robot 192.168.58.2  # 실제 로봇 연결
"""

import asyncio
import json
import ssl
import math
import time
import os
import sys
import argparse
import logging
from pathlib import Path

import numpy as np
from aiohttp import web

# ──────────────────────────────────────────────
#  설정
# ──────────────────────────────────────────────
DEFAULT_PORT = 8443
SERVO_PERIOD = 0.008          # 8ms (125Hz)
POSITION_SCALE = 500.0        # VR 1m 이동 → 로봇 500mm 이동 (조정 가능)
MAX_DELTA_PER_STEP = 2.0      # 한 스텝 최대 이동량 mm (안전)
WORKSPACE_BOUNDS = {           # 로봇 작업 공간 제한 (mm)
    'x': (-600, 600),
    'y': (-600, 600),
    'z': (50, 700),
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('teleop')

def _update_scale(new_scale):
    global POSITION_SCALE
    POSITION_SCALE = new_scale


# ──────────────────────────────────────────────
#  수학 유틸리티
# ──────────────────────────────────────────────
def quat_conjugate(q):
    """쿼터니언 켤레 (역회전)"""
    return np.array([-q[0], -q[1], -q[2], q[3]])

def quat_multiply(q1, q2):
    """쿼터니언 곱셈 q1 * q2"""
    x1, y1, z1, w1 = q1
    x2, y2, z2, w2 = q2
    return np.array([
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2,
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
    ])

def quat_to_euler_xyz(q):
    """쿼터니언 → Euler XYZ (degrees), Fairino 컨벤션"""
    x, y, z, w = q
    # Roll (X)
    sinr = 2.0 * (w * x + y * z)
    cosr = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr, cosr)
    # Pitch (Y)
    sinp = 2.0 * (w * y - z * x)
    sinp = max(-1.0, min(1.0, sinp))
    pitch = math.asin(sinp)
    # Yaw (Z)
    siny = 2.0 * (w * z + x * y)
    cosy = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny, cosy)
    return np.array([math.degrees(roll), math.degrees(pitch), math.degrees(yaw)])


# ──────────────────────────────────────────────
#  텔레옵 브릿지
# ──────────────────────────────────────────────
class TeleopBridge:
    def __init__(self, robot_ip=None, dry_run=True):
        self.dry_run = dry_run
        self.robot = None
        self.robot_ip = robot_ip
        self.servo_active = False

        # 클러칭 상태
        self.is_tracking = False
        self.vr_origin_pos = None
        self.vr_origin_quat = None
        self.robot_origin_tcp = None
        self.grip_pressed = False
        self.last_send_time = 0
        self.last_target_tcp = None

        # 통계
        self.frame_count = 0
        self.connected_clients = 0
        self.total_received = 0

        # 노이즈 및 떨림(Jittering) 방지 필터링
        self.filtered_pos = None
        self.ema_alpha = 0.25      # VR 노이즈 평활화 계수 (0.1~0.3)
        self.max_step_mm = 1.5     # 8ms 스텝당 최대 이동거리 제한 (약 187.5 mm/s)

    # ── 로봇 연결 ──
    def connect_robot(self):
        if self.dry_run:
            log.info("🤖 드라이런 모드 — 로봇 미연결, 콘솔 출력만 합니다")
            self.robot_origin_tcp = [300.0, 0.0, 400.0, 180.0, 0.0, 0.0]
            return True
        try:
            sdk_path = str(Path(__file__).resolve().parent.parent
                           / 'fairino-python-sdk-main' / 'linux')
            if sdk_path not in sys.path:
                sys.path.insert(0, sdk_path)
            from fairino import Robot
            self.robot = Robot.RPC(self.robot_ip)
            log.info(f"🤖 FR5 연결 성공: {self.robot_ip}")

            # 로봇 에러 리셋, 자동 모드(0), 드래그 티칭 해제(0), 서보 Enable(1)
            err_reset = self.robot.ResetAllError()
            err_mode = self.robot.Mode(0)
            err_drag = self.robot.DragTeachSwitch(0)
            err_enable = self.robot.RobotEnable(1)
            log.info(f"⚙️ 로봇 초기화: ResetError={err_reset}, Mode(0)={err_mode}, DragOff={err_drag}, Enable={err_enable}")

            return True
        except Exception as e:
            log.error(f"❌ 로봇 연결 실패: {e}")
            return False

    def start_servo(self):
        if self.servo_active:
            return
        if self.robot and not self.dry_run:
            err = self.robot.ServoMoveStart()
            if err != 0:
                log.error(f"⚠️ ServoMoveStart 1차 실패 (Error {err}) → 로봇 상태 재설정 및 재시도 중...")
                self.robot.ResetAllError()
                self.robot.Mode(0)
                self.robot.RobotEnable(1)
                time.sleep(0.1)
                err = self.robot.ServoMoveStart()
                log.info(f"🔄 ServoMoveStart 재시도 결과 → {err}")
            else:
                log.info(f"ServoMoveStart → {err}")
        self.servo_active = True

    def stop_servo(self):
        if not self.servo_active:
            return
        if self.robot and not self.dry_run:
            err = self.robot.ServoMoveEnd()
            log.info(f"ServoMoveEnd → {err}")
        self.servo_active = False

    def get_current_tcp(self):
        if self.dry_run:
            return list(self.robot_origin_tcp or [300, 0, 400, 180, 0, 0])
        if self.robot:
            err, tcp = self.robot.GetActualTCPPose()
            if err == 0:
                return list(tcp)
        return None

    # ── VR→로봇 좌표 변환 ──
    def vr_to_robot_delta(self, vr_delta_m):
        """VR 미터 델타 → 로봇 mm 델타 (축 매핑 포함)"""
        dx, dy, dz = vr_delta_m
        # VR(Y-up, Z-toward-user) → Robot(Z-up) 기본 매핑
        # 로봇 앞에서 VR을 사용한다고 가정
        robot_dx = -dz * POSITION_SCALE   # VR 앞/뒤 → 로봇 X
        robot_dy = -dx * POSITION_SCALE   # VR 좌/우 → 로봇 Y
        robot_dz =  dy * POSITION_SCALE   # VR 위/아래 → 로봇 Z
        return np.array([robot_dx, robot_dy, robot_dz])

    def clamp_velocity(self, delta):
        """한 스텝 최대 이동량 제한"""
        norm = np.linalg.norm(delta)
        if norm > MAX_DELTA_PER_STEP:
            delta = delta * (MAX_DELTA_PER_STEP / norm)
        return delta

    def clamp_workspace(self, tcp):
        """작업 공간 범위 클램핑"""
        tcp[0] = float(np.clip(tcp[0], *WORKSPACE_BOUNDS['x']))
        tcp[1] = float(np.clip(tcp[1], *WORKSPACE_BOUNDS['y']))
        tcp[2] = float(np.clip(tcp[2], *WORKSPACE_BOUNDS['z']))
        return tcp

    # ── 메인 업데이트 ──
    def update(self, data):
        """VR 프레임 데이터 처리 + 로봇 명령 전송"""
        self.total_received += 1

        pos = np.array(data.get('position', [0, 0, 0]), dtype=float)
        quat = np.array(data.get('orientation', [0, 0, 0, 1]), dtype=float)
        grip = data.get('grip', False)
        trigger = data.get('trigger', False)

        if self.total_received % 100 == 0:
            log.info(f"📡 지속 수신 중... (Grip: {grip}, Trigger: {trigger}, Pos: {pos[0]:.2f}, {pos[1]:.2f})")

        # VR 위치 Low-Pass Filter (EMA) 적용하여 미세 손떨림/노이즈 제거
        if self.filtered_pos is None or not self.is_tracking:
            self.filtered_pos = pos.copy()
        else:
            self.filtered_pos = self.ema_alpha * pos + (1.0 - self.ema_alpha) * self.filtered_pos

        # 클러칭: 그립 누르는 순간 원점 기록
        if grip and not self.grip_pressed:
            self.vr_origin_pos = self.filtered_pos.copy()
            self.vr_origin_quat = quat.copy()
            tcp = self.get_current_tcp()
            if tcp:
                self.robot_origin_tcp = tcp
            self.is_tracking = True
            if not self.servo_active:
                self.start_servo()
            log.info(f"🟢 추적 시작 | 로봇 원점: "
                     f"[{self.robot_origin_tcp[0]:.1f}, "
                     f"{self.robot_origin_tcp[1]:.1f}, "
                     f"{self.robot_origin_tcp[2]:.1f}]")

        elif not grip and self.grip_pressed:
            self.is_tracking = False
            self.stop_servo()
            log.info("🔴 추적 중지 (그립 해제)")

        self.grip_pressed = grip

        # ── 추적 중이면 로봇에 명령 전송 ──
        if not self.is_tracking or self.robot_origin_tcp is None:
            return self._build_status(pos)

        now = time.time()
        if now - self.last_send_time < SERVO_PERIOD:
            return self._build_status(pos)

        # 위치 델타 (필터링된 VR 원점 기준 손의 원하는 이동량)
        vr_delta = self.filtered_pos - self.vr_origin_pos
        desired_robot_delta = self.vr_to_robot_delta(vr_delta)
        desired_target = np.array(self.robot_origin_tcp[:3]) + desired_robot_delta

        # 이전 타겟 위치 대비 한 스텝(8ms)당 이동량 제한 (Max step limit: 1.5mm/8ms)
        prev_target = np.array(self.last_target_tcp[:3]) if self.last_target_tcp else np.array(self.robot_origin_tcp[:3])
        step_delta = desired_target - prev_target
        step_norm = np.linalg.norm(step_delta)

        if step_norm > self.max_step_mm:
            step_delta = step_delta * (self.max_step_mm / step_norm)

        actual_target = prev_target + step_delta

        # 목표 TCP 계산
        target = list(self.robot_origin_tcp)
        target[0] = float(actual_target[0])
        target[1] = float(actual_target[1])
        target[2] = float(actual_target[2])

        # 자세는 원점 유지 (위치 텔레옵만, 추후 회전 추가 가능)
        target = self.clamp_workspace(target)
        self.last_target_tcp = target
        self.last_send_time = now
        self.frame_count += 1

        # 로봇 전송
        if self.dry_run:
            if self.frame_count % 50 == 0:  # 0.4초마다 출력
                log.info(f"📍 Target TCP: [{target[0]:.1f}, {target[1]:.1f}, "
                         f"{target[2]:.1f}, {target[3]:.1f}, {target[4]:.1f}, "
                         f"{target[5]:.1f}]")
        else:
            err = self.robot.ServoCart(
                mode=0,
                desc_pos=target,
                pos_gain=[1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
                acc=100.0,
                vel=100.0,
                cmdT=SERVO_PERIOD,
                filterT=0.05
            )
            if self.frame_count % 50 == 0 or err != 0:
                log.info(f"📍 Target TCP: [{target[0]:.1f}, {target[1]:.1f}, {target[2]:.1f}] | ServoCart err: {err}")

        return self._build_status(pos)

    def _build_status(self, vr_pos):
        return {
            'type': 'status',
            'tracking': self.is_tracking,
            'robot_tcp': self.last_target_tcp,
            'vr_pos': vr_pos.tolist() if isinstance(vr_pos, np.ndarray) else vr_pos,
            'frame': self.frame_count,
        }

    def shutdown(self):
        if self.servo_active:
            self.stop_servo()


# ──────────────────────────────────────────────
#  웹 서버 (HTTPS + WebSocket)
# ──────────────────────────────────────────────
bridge: TeleopBridge = None

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    bridge.connected_clients += 1
    client_ip = request.remote
    log.info(f"🔗 WebSocket 연결: {client_ip} (총 {bridge.connected_clients}명)")

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get('type') == 'pose':
                        status = bridge.update(data)
                        if status:
                            await ws.send_json(status)
                    elif data.get('type') == 'event':
                        evt = data.get('event')
                        if evt == 'webxr_started':
                            log.info(f"🥽 WebXR 세션 활성화 (VR 모드 진입): {client_ip}")
                        elif evt == 'webxr_ended':
                            log.info(f"🕶️ WebXR 세션 종료 (브라우저 복귀): {client_ip}")
                except json.JSONDecodeError:
                    pass
            elif msg.type == web.WSMsgType.ERROR:
                log.error(f"WebSocket 에러: {ws.exception()}")
    finally:
        bridge.connected_clients -= 1
        if bridge.connected_clients <= 0:
            bridge.is_tracking = False
            bridge.grip_pressed = False
        log.info(f"🔌 WebSocket 해제: {client_ip}")

    return ws

async def index_handler(request):
    """WebXR 페이지 제공"""
    web_dir = Path(__file__).parent / 'web'
    return web.FileResponse(web_dir / 'index.html')

async def on_shutdown(app):
    log.info("서버 종료 중...")
    bridge.shutdown()

def create_ssl_context(cert_dir):
    cert = Path(cert_dir) / 'cert.pem'
    key = Path(cert_dir) / 'key.pem'
    if not cert.exists() or not key.exists():
        log.error(f"❌ SSL 인증서가 없습니다. 먼저 setup.sh를 실행하세요.")
        sys.exit(1)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    return ctx

def main():
    global bridge

    parser = argparse.ArgumentParser(description='VR Teleop Bridge for FR5')
    parser.add_argument('--robot', type=str, default=None,
                        help='로봇 IP (미지정시 드라이런)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--scale', type=float, default=POSITION_SCALE,
                        help=f'위치 스케일 (기본 {POSITION_SCALE})')
    args = parser.parse_args()

    # 스케일 업데이트
    _update_scale(args.scale)

    dry_run = args.robot is None
    bridge = TeleopBridge(robot_ip=args.robot, dry_run=dry_run)
    bridge.connect_robot()

    # SSL
    cert_dir = Path(__file__).parent / 'certs'
    ssl_ctx = create_ssl_context(cert_dir)

    # aiohttp 앱
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/ws', websocket_handler)
    app.router.add_static('/static',
                          Path(__file__).parent / 'web',
                          show_index=False)
    app.on_shutdown.append(on_shutdown)

    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    # 더 정확한 IP 가져오기
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print()
    print("=" * 50)
    print("  VR Teleop Bridge for Fairino FR5")
    print("=" * 50)
    print(f"  모드:     {'🔴 드라이런' if dry_run else '🟢 실제 로봇'}")
    if not dry_run:
        print(f"  로봇 IP:  {args.robot}")
    print(f"  스케일:   VR 1m → 로봇 {POSITION_SCALE:.0f}mm")
    print(f"  서버:     https://{local_ip}:{args.port}")
    print()
    print(f"  👉 Quest 3 브라우저에서 위 URL 접속!")
    print("=" * 50)
    print()

    web.run_app(app, host='0.0.0.0', port=args.port,
                ssl_context=ssl_ctx, print=None)

if __name__ == '__main__':
    main()
