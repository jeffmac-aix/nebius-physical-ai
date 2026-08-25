"""Live Franka camera -> OpenPI -> safe target loop for Antioch."""

from __future__ import annotations

import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import antioch

logger = antioch.Logger("openpi-live")

CLIENT_ROOT = Path("/tmp/npa-live-client-current")
ACTION_SHAPE = (15, 8)
CONTROL_HZ = 15.0
TARGETS_PER_QUERY = 5
# A cold B200 model request can take tens of seconds even though warmed requests
# are normally tens of milliseconds. This is a stale-response safety deadline,
# not a real-time claim or a total run limit.
MAX_RESPONSE_AGE_SECONDS = 90.0
JOINT_LOW = (-2.8973, -1.7628, -2.8973, -3.0718, -2.8973, -0.0175, -2.8973)
JOINT_HIGH = (2.8973, 1.7628, 2.8973, -0.0698, 2.8973, 3.7525, 2.8973)
MAX_JOINT_STEP = 0.35
GRIPPER_JOINT_MAX = 0.04


class ActionValidationError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SafePolicyClient:
    """Authenticated WSS client for the persistent service-side bridge."""

    def __init__(self) -> None:
        self._connection = None
        self.reconnects = 0
        self._backoff = 1.0

    def _settings(self) -> tuple[str, str, ssl.SSLContext]:
        token = (CLIENT_ROOT / "relay-api-key").read_text().strip()
        if len(token) < 32:
            raise RuntimeError("policy relay API key is missing or malformed")
        context = ssl.create_default_context(cafile=str(CLIENT_ROOT / "relay-ca.crt"))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return "wss://127.0.0.1:8444", token, context

    def connect(self) -> None:
        import openpi_protocol
        from websockets.sync.client import connect

        self.close()
        uri, token, context = self._settings()
        self._connection = connect(
            uri,
            ssl=context,
            compression=None,
            max_size=32 * 1024 * 1024,
            max_queue=2,
            open_timeout=10,
            close_timeout=5,
            additional_headers={
                "Authorization": f"Api-Key {token}",
                "X-NPA-Relay-Role": "simulation",
            },
            proxy=None,
        )
        greeting = self._connection.recv(timeout=30)
        metadata = openpi_protocol.unpackb(greeting)
        if not isinstance(metadata, dict):
            raise RuntimeError("policy server greeting is malformed")
        self._backoff = 1.0

    def infer(self, observation: dict) -> tuple[dict, float]:
        import openpi_protocol

        if self._connection is None:
            self.connect()
        started = time.monotonic()
        try:
            self._connection.send(openpi_protocol.Packer().pack(observation))
            payload = self._connection.recv(timeout=MAX_RESPONSE_AGE_SECONDS)
            result = openpi_protocol.unpackb(payload)
        except Exception:
            self.close()
            raise
        latency = time.monotonic() - started
        if not isinstance(result, dict):
            self.close()
            raise RuntimeError("policy response is not an object")
        return result, latency

    def reconnect_delay(self) -> float:
        self.reconnects += 1
        delay = self._backoff
        self._backoff = min(self._backoff * 2.0, 30.0)
        return delay

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def shutdown(self) -> None:
        self.close()


def _look_at(stage, path: str, eye, target) -> None:
    import numpy as np
    from pxr import Gf, UsdGeom

    forward = np.asarray(target, dtype=float) - np.asarray(eye, dtype=float)
    forward /= max(float(np.linalg.norm(forward)), 1e-9)
    right = np.cross(forward, np.array([0.0, 0.0, 1.0]))
    right /= max(float(np.linalg.norm(right)), 1e-9)
    up = np.cross(right, forward)
    matrix = Gf.Matrix4d(1.0)
    matrix.SetRow3(0, Gf.Vec3d(*right))
    matrix.SetRow3(1, Gf.Vec3d(*up))
    matrix.SetRow3(2, Gf.Vec3d(*(-forward)))
    matrix.SetTranslateOnly(Gf.Vec3d(*eye))
    transform = UsdGeom.Xformable(stage.GetPrimAtPath(path))
    transform.ClearXformOpOrder()
    transform.AddTransformOp().Set(matrix)


def _validated_actions(response: dict, current) -> tuple[object, int]:
    import numpy as np

    actions = np.asarray(response.get("actions"))
    if actions.shape != ACTION_SHAPE:
        raise ActionValidationError("wrong_shape")
    if not np.issubdtype(actions.dtype, np.number) or not np.isfinite(actions).all():
        raise ActionValidationError("non_finite")
    targets = actions.astype(np.float64, copy=True)
    low, high = np.asarray(JOINT_LOW), np.asarray(JOINT_HIGH)
    if np.any(targets[:, :7] < low) or np.any(targets[:, :7] > high):
        raise ActionValidationError("joint_limit")
    # Upstream DroidOutputs returns the eighth model dimension unchanged. Keep
    # the finite check strict, then saturate that command to the actuator's
    # reviewed normalized range before mapping it to finger joint positions.
    gripper_saturations = int(
        np.count_nonzero((targets[:, 7] < 0.0) | (targets[:, 7] > 1.0))
    )
    targets[:, 7] = np.clip(targets[:, 7], 0.0, 1.0)
    prior = np.asarray(current[:7], dtype=np.float64)
    deltas = np.diff(np.vstack([prior, targets[:, :7]]), axis=0)
    if np.max(np.abs(deltas)) > MAX_JOINT_STEP:
        raise ActionValidationError("joint_step")
    return targets, gripper_saturations


def _install_overlay():
    import omni.ui as ui

    window = ui.Window("NPA OpenPI live decisions", width=520, height=250)
    with window.frame:
        with ui.VStack(spacing=4):
            title = ui.Label("LIVE camera → pi0.5 DROID → Franka", height=28)
            state = ui.Label("initializing", word_wrap=True)
            counters = ui.Label("", word_wrap=True)
            latency = ui.Label("", word_wrap=True)
    return window, title, state, counters, latency


def _camera_rgb(camera):
    """Return a validated RGB frame, or None while the annotator warms up."""

    import numpy as np

    rgba = camera.get_rgba()
    if rgba is None:
        return None
    frame = np.asarray(rgba)
    if frame.ndim != 3 or frame.shape[2] < 3 or frame.size == 0:
        return None
    if not np.issubdtype(frame.dtype, np.number) or not np.isfinite(frame).all():
        return None
    return frame[:, :, :3].astype(np.uint8, copy=False)


@antioch.scenario(tags=["openpi-live"])
def openpi_droid_live(
    run: antioch.ScenarioRun,
    prompt: str = antioch.param("pick up the cube", description="DROID task prompt"),
) -> None:
    """Continuously render, infer, validate and apply pi0.5 action chunks."""

    import numpy as np
    import rerun as rr
    from isaacsim.core.api.objects import DynamicCuboid
    from isaacsim.core.utils.types import ArticulationAction
    from isaacsim.core.utils.viewports import set_camera_view
    from isaacsim.core.utils.extensions import enable_extension

    # Isaac Sim 6 keeps the legacy Franka helper as an opt-in extension.
    enable_extension("isaacsim.robot.manipulators.examples")
    from isaacsim.robot.manipulators.examples.franka import Franka
    from isaacsim.sensors.camera import Camera

    world = antioch.world()
    world.scene.add_ground_plane()
    robot = world.scene.add(Franka(prim_path="/World/Franka", name="franka"))
    cube = world.scene.add(
        DynamicCuboid(
            prim_path="/World/Cube",
            name="cube",
            position=np.array([0.48, 0.0, 0.035]),
            size=0.07,
            color=np.array([0.85, 0.12, 0.08]),
        )
    )
    exterior = Camera(
        prim_path="/World/PolicyExterior",
        position=np.array([1.25, -1.05, 0.85]),
        resolution=(224, 224),
    )
    wrist = Camera(
        prim_path="/World/PolicyWrist",
        position=np.array([0.65, -0.38, 0.42]),
        resolution=(224, 224),
    )
    world.reset()
    exterior.initialize()
    wrist.initialize()
    _look_at(
        world.stage, "/World/PolicyExterior", [1.25, -1.05, 0.85], [0.45, 0.0, 0.1]
    )
    _look_at(world.stage, "/World/PolicyWrist", [0.65, -0.38, 0.42], [0.45, 0.0, 0.08])
    set_camera_view(
        eye=[1.55, -1.3, 0.95],
        target=[0.42, 0.08, 0.12],
        camera_prim_path="/OmniverseKit_Persp",
    )
    overlay = _install_overlay()
    client = SafePolicyClient()
    observation_sequence = requests = round_trips = applied = safe_holds = 0
    gripper_saturations = 0
    last_latency = 0.0
    started = time.monotonic()
    next_attempt = 0.0
    chunk = None
    chunk_index = 0
    last_apply = time.monotonic()
    first_frame = True
    pending = None
    pending_observation = 0
    pending_joint_positions = None
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="openpi-policy")

    print("NPA_OPENPI_LOOP_READY", flush=True)
    try:
        while True:
            world.step(render=True)
            now = time.monotonic()

            if pending is not None and pending.done():
                try:
                    response, last_latency = pending.result()
                    if last_latency > MAX_RESPONSE_AGE_SECONDS:
                        raise TimeoutError("policy response was stale")
                    chunk, saturated = _validated_actions(
                        response, pending_joint_positions
                    )
                    gripper_saturations += saturated
                    chunk_index = 0
                    round_trips += 1
                    print(
                        "NPA_OPENPI_ROUND_TRIP "
                        f"observation={pending_observation} "
                        f"round_trips={round_trips} "
                        f"latency_ms={last_latency * 1000.0:.3f} "
                        "action_shape=[15,8] finite=true safety_validated=true "
                        f"gripper_saturations={gripper_saturations}",
                        flush=True,
                    )
                except Exception as exc:
                    safe_holds += 1
                    reason = (
                        exc.reason
                        if isinstance(exc, ActionValidationError)
                        else type(exc).__name__
                    )
                    if isinstance(exc, ActionValidationError):
                        next_attempt = now + 1.0 / CONTROL_HZ
                    else:
                        client.close()
                        next_attempt = now + client.reconnect_delay()
                    logger.value("policy/error", rr.TextLog(reason))
                    print(
                        "NPA_OPENPI_SAFE_HOLD "
                        f"observation={pending_observation} "
                        f"reason={reason} "
                        f"reconnects={client.reconnects}",
                        flush=True,
                    )
                finally:
                    pending = None
                    pending_joint_positions = None

            if chunk is None and pending is None and now >= next_attempt:
                joint_positions = np.asarray(
                    robot.get_joint_positions(), dtype=np.float32
                )
                exterior_rgb = _camera_rgb(exterior)
                wrist_rgb = _camera_rgb(wrist)
                if exterior_rgb is None or wrist_rgb is None:
                    safe_holds += 1
                    next_attempt = now + 1.0 / CONTROL_HZ
                    overlay[2].text = "SAFE HOLD / waiting for camera frames"
                else:
                    if first_frame:
                        print("NPA_OPENPI_FIRST_FRAME", flush=True)
                        first_frame = False
                    observation_sequence += 1
                    observation = {
                        "observation/exterior_image_1_left": exterior_rgb,
                        "observation/wrist_image_left": wrist_rgb,
                        "observation/joint_position": joint_positions[:7],
                        "observation/gripper_position": np.asarray(
                            [float(np.sum(joint_positions[7:9]))], dtype=np.float32
                        ),
                        "prompt": prompt,
                    }
                    requests += 1
                    print(
                        "NPA_OPENPI_REQUEST "
                        f"observation={observation_sequence} requests={requests}",
                        flush=True,
                    )
                    logger.image("camera/exterior", exterior_rgb)
                    logger.image("camera/wrist", wrist_rgb)
                    pending_observation = observation_sequence
                    pending_joint_positions = joint_positions.copy()
                    pending = executor.submit(client.infer, observation)

            if chunk is not None and now - last_apply >= 1.0 / CONTROL_HZ:
                target = chunk[chunk_index]
                robot.apply_action(
                    ArticulationAction(
                        joint_positions=np.concatenate(
                            [
                                target[:7],
                                np.repeat(target[7] * GRIPPER_JOINT_MAX, 2),
                            ]
                        )
                    )
                )
                applied += 1
                chunk_index += 1
                last_apply = now
                print(
                    f"NPA_OPENPI_APPLIED applied={applied} chunk_index={chunk_index}",
                    flush=True,
                )
                if chunk_index >= TARGETS_PER_QUERY:
                    chunk = None

            safe_hold = chunk is None
            logger.scalar("decision/observation_sequence", observation_sequence)
            logger.scalar("decision/observation_time_seconds", now - started)
            logger.scalar("decision/policy_requests", requests)
            logger.scalar("decision/policy_in_flight", int(pending is not None))
            logger.scalar("decision/round_trips", round_trips)
            logger.scalar("decision/inference_latency_ms", last_latency * 1000.0)
            logger.scalar("decision/action_horizon", ACTION_SHAPE[0])
            logger.scalar("decision/action_dimension", ACTION_SHAPE[1])
            logger.scalar("decision/chunk_index", chunk_index)
            logger.scalar("decision/safe_hold", int(safe_hold))
            logger.scalar("decision/reconnects", client.reconnects)
            logger.scalar("decision/safe_targets_applied", applied)
            logger.scalar("decision/gripper_saturations", gripper_saturations)
            logger.scalar(
                "decision/applied_target_rate_hz",
                applied / max(now - started, 1e-6),
            )
            logger.value(
                "scene/cube",
                rr.Boxes3D(
                    centers=[cube.get_world_pose()[0].tolist()],
                    sizes=[[0.07, 0.07, 0.07]],
                ),
            )
            overlay[2].text = (
                "SAFE HOLD / reconnecting"
                if safe_hold
                else "VALIDATED ACTIONS APPLYING"
            )
            overlay[3].text = (
                f"obs {observation_sequence} | requests {requests} | round trips {round_trips}\n"
                f"action [15,8], row {chunk_index} | applied {applied} | reconnects {client.reconnects}"
            )
            overlay[
                4
            ].text = f"last inference {last_latency * 1000.0:.1f} ms | safe holds {safe_holds}"
    finally:
        client.shutdown()
        executor.shutdown(wait=False, cancel_futures=True)
        run.add_result("observation_sequence", observation_sequence)
        run.add_result("policy_requests", requests)
        run.add_result("policy_round_trips", round_trips)
        run.add_result("safe_targets_applied", applied)
        run.add_result("reconnects", client.reconnects)
