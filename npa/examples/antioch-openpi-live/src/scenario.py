"""Live Franka camera -> OpenPI -> safe target loop for Antioch."""

from __future__ import annotations

import math
import ssl
import time
from collections import Counter
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
GRIPPER_TOTAL_WIDTH_MAX = 2.0 * GRIPPER_JOINT_MAX
DROID_RESET_JOINTS = (
    0.0,
    -math.pi / 5.0,
    0.0,
    -4.0 * math.pi / 5.0,
    0.0,
    3.0 * math.pi / 5.0,
    0.0,
)
TASK_LABEL = "red_cube_pickup"
CUBE_SIZE_METERS = 0.07
CUBE_INITIAL_POSITION = (0.48, 0.0, CUBE_SIZE_METERS / 2.0)
PICKUP_LIFT_METERS = 0.05
PICKUP_HOLD_SECONDS = 1.0
GRIPPER_CONTACT_FORCE_NEWTONS = 0.1
MIN_CAMERA_LUMINANCE_MEAN = 5.0
MIN_CAMERA_LUMINANCE_VARIANCE = 25.0


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


def _configure_lighting(stage) -> None:
    """Create deterministic fill and key lights for both policy cameras."""

    from pxr import Gf, UsdGeom, UsdLux

    dome = UsdLux.DomeLight.Define(stage, "/World/PolicyFillLight")
    dome.CreateIntensityAttr(900.0)
    dome.CreateExposureAttr(1.0)
    dome.CreateColorAttr(Gf.Vec3f(1.0, 0.98, 0.95))

    key = UsdLux.DistantLight.Define(stage, "/World/PolicyKeyLight")
    key.CreateIntensityAttr(2_500.0)
    key.CreateExposureAttr(1.0)
    key.CreateAngleAttr(4.0)
    key.CreateColorAttr(Gf.Vec3f(1.0, 0.96, 0.9))
    transform = UsdGeom.Xformable(key.GetPrim())
    transform.AddRotateXYZOp().Set(Gf.Vec3f(-35.0, -25.0, -35.0))


def _contact_force_magnitude(contact_view, physics_dt: float) -> float:
    """Read gripper/cube contact from Isaac's tracked-contact view."""

    import numpy as np

    forces = contact_view.get_contact_force_matrix(dt=physics_dt)
    if forces is None:
        return 0.0
    if hasattr(forces, "detach"):
        forces = forces.detach().cpu().numpy()
    elif hasattr(forces, "numpy"):
        forces = forces.numpy()
    array = np.asarray(forces, dtype=np.float64)
    if array.size == 0 or not np.isfinite(array).all():
        return 0.0
    return float(np.linalg.norm(array, axis=-1).max(initial=0.0))


def _droid_gripper_observation(joint_positions) -> float:
    """Map Isaac finger opening to DROID's 0=open, 1=closed convention."""

    import numpy as np

    fingers = np.asarray(joint_positions, dtype=np.float64)[7:9]
    if fingers.shape != (2,) or not np.isfinite(fingers).all():
        raise ActionValidationError("non_finite")
    opening_width = float(np.clip(fingers.sum(), 0.0, GRIPPER_TOTAL_WIDTH_MAX))
    return 1.0 - opening_width / GRIPPER_TOTAL_WIDTH_MAX


def _isaac_finger_target(droid_gripper_target: float) -> float:
    """Map DROID gripper position to one Isaac finger joint target."""

    # DROID and the pinned OpenPI policy use 0=open and 1=closed.  The stock
    # Franka articulation is the inverse: each finger is 0.04 m open and 0 m
    # closed.  Upstream's deployment example binarizes the model target.
    closed = float(droid_gripper_target) > 0.5
    return 0.0 if closed else GRIPPER_JOINT_MAX


def _validated_actions(response: dict, current) -> tuple[object, dict[str, int]]:
    import numpy as np

    actions = np.asarray(response.get("actions"))
    if actions.shape != ACTION_SHAPE:
        raise ActionValidationError("wrong_shape")
    if not np.issubdtype(actions.dtype, np.number) or not np.isfinite(actions).all():
        raise ActionValidationError("non_finite")
    targets = actions.astype(np.float64, copy=True)
    low, high = np.asarray(JOINT_LOW), np.asarray(JOINT_HIGH)
    raw_joint_limit_mismatches = int(
        np.count_nonzero((targets[:, :7] < low) | (targets[:, :7] > high))
    )
    targets[:, :7] = np.clip(targets[:, :7], low, high)
    # The pinned Polaris data configuration trains absolute joint positions and
    # DROID gripper position. DroidOutputs intentionally returns both unchanged.
    # Preserve raw distribution evidence, but use upstream's documented binary
    # gripper mapping instead of treating an unbounded model value as finger
    # opening in metres.
    raw_gripper_range_mismatches = int(
        np.count_nonzero((targets[:, 7] < 0.0) | (targets[:, 7] > 1.0))
    )
    targets[:, 7] = (targets[:, 7] > 0.5).astype(np.float64)
    prior = np.asarray(current[:7], dtype=np.float64)
    joint_step_projections = 0
    for index in range(ACTION_SHAPE[0]):
        bounded = np.clip(
            targets[index, :7], prior - MAX_JOINT_STEP, prior + MAX_JOINT_STEP
        )
        joint_step_projections += int(np.count_nonzero(bounded != targets[index, :7]))
        targets[index, :7] = bounded
        prior = bounded
    return targets, {
        "raw_gripper_range_mismatches": raw_gripper_range_mismatches,
        "raw_joint_limit_mismatches": raw_joint_limit_mismatches,
        "joint_limit_projections": raw_joint_limit_mismatches,
        "joint_step_projections": joint_step_projections,
    }


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
    """Return a rendered RGB frame, or None while the annotator warms up."""

    import numpy as np

    rgba = camera.get_rgba()
    if rgba is None:
        return None
    frame = np.asarray(rgba)
    if frame.ndim != 3 or frame.shape[2] < 3 or frame.size == 0:
        return None
    if not np.issubdtype(frame.dtype, np.number) or not np.isfinite(frame).all():
        return None
    rgb_source = frame[:, :, :3]
    if np.issubdtype(rgb_source.dtype, np.floating):
        upper = float(rgb_source.max())
        if upper <= 1.0:
            rgb_source = rgb_source * 255.0
    rgb = np.clip(rgb_source, 0, 255).astype(np.uint8, copy=False)
    luminance = np.mean(rgb, axis=2)
    if (
        float(luminance.mean()) <= MIN_CAMERA_LUMINANCE_MEAN
        or float(luminance.var()) <= MIN_CAMERA_LUMINANCE_VARIANCE
    ):
        return None
    return rgb


def _franka_link_points(stage):
    """Read the rendered Franka link transforms for live Rerun geometry."""

    from pxr import Usd, UsdGeom

    points = []
    for name in [*(f"panda_link{index}" for index in range(8)), "panda_hand"]:
        prim = stage.GetPrimAtPath(f"/World/Franka/{name}")
        if not prim or not prim.IsValid() or not prim.IsA(UsdGeom.Xformable):
            continue
        transform = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(
            Usd.TimeCode.Default()
        )
        points.append(list(transform.ExtractTranslation()))
    return points


def _unit_vector(vector):
    length = math.sqrt(sum(component * component for component in vector))
    if length <= 1e-9:
        return None
    return [component / length for component in vector]


def _z_axis_quaternion(direction):
    """Return an xyzw quaternion that rotates local +Z onto direction."""

    unit = _unit_vector(direction)
    if unit is None:
        raise ValueError("cannot orient geometry along a zero-length vector")
    dot = max(-1.0, min(1.0, unit[2]))
    if dot < -1.0 + 1e-8:
        return [1.0, 0.0, 0.0, 0.0]
    quaternion = [-unit[1], unit[0], 0.0, 1.0 + dot]
    norm = math.sqrt(sum(component * component for component in quaternion))
    return [component / norm for component in quaternion]


def _franka_proxy_geometry(link_points):
    """Build a volumetric Franka proxy from live USD link translations.

    The proxy intentionally contains only generated primitives. The Isaac/Franka
    mesh remains inside the operator-accepted runtime and is visible through the
    two rendered camera streams, while Rerun gets recognizable moving geometry
    without copying any simulator asset bytes.
    """

    points = [[float(component) for component in point] for point in link_points]
    links = {"centers": [], "sizes": [], "quaternions": [], "colors": []}
    last_direction = None
    for index, (start, end) in enumerate(zip(points, points[1:])):
        direction = [right - left for left, right in zip(start, end)]
        unit = _unit_vector(direction)
        if unit is None:
            continue
        length = math.sqrt(sum(component * component for component in direction))
        width = 0.105 if index < 2 else 0.085 if index < 5 else 0.065
        links["centers"].append(
            [(left + right) / 2.0 for left, right in zip(start, end)]
        )
        links["sizes"].append([width, width, length])
        links["quaternions"].append(_z_axis_quaternion(direction))
        links["colors"].append(
            [230, 232, 235, 255] if index % 2 == 0 else [195, 200, 206, 255]
        )
        last_direction = unit

    if not points:
        return {"base": None, "links": links, "joints": None, "gripper": None}

    base = {
        "centers": [[points[0][0], points[0][1], points[0][2] - 0.055]],
        "sizes": [[0.20, 0.20, 0.11]],
        "colors": [[58, 63, 70, 255]],
    }
    joints = {
        "centers": points,
        "radii": [0.057 if index < 2 else 0.046 for index in range(len(points))],
        "colors": [[40, 44, 52, 255]] * len(points),
    }
    if last_direction is None:
        return {"base": base, "links": links, "joints": joints, "gripper": None}

    up = [0.0, 0.0, 1.0]
    lateral = _unit_vector(
        [
            last_direction[1] * up[2] - last_direction[2] * up[1],
            last_direction[2] * up[0] - last_direction[0] * up[2],
            last_direction[0] * up[1] - last_direction[1] * up[0],
        ]
    )
    if lateral is None:
        lateral = [1.0, 0.0, 0.0]
    hand = points[-1]
    orientation = _z_axis_quaternion(last_direction)
    palm_center = [value + 0.025 * axis for value, axis in zip(hand, last_direction)]
    finger_centers = []
    for sign in (-1.0, 1.0):
        finger_centers.append(
            [
                value + 0.10 * axis + sign * 0.047 * side
                for value, axis, side in zip(hand, last_direction, lateral)
            ]
        )
    gripper = {
        "centers": [palm_center, *finger_centers],
        "sizes": [[0.14, 0.09, 0.055], [0.024, 0.024, 0.15], [0.024, 0.024, 0.15]],
        "quaternions": [orientation, orientation, orientation],
        "colors": [[58, 63, 70, 255], [34, 39, 48, 255], [34, 39, 48, 255]],
    }
    return {"base": base, "links": links, "joints": joints, "gripper": gripper}


@antioch.scenario(tags=["openpi-live", "mk8s-native"])
def openpi_franka_mk8s_live_v2(
    run: antioch.ScenarioRun,
    prompt: str = antioch.param(
        "pick up the red cube", description="DROID task prompt"
    ),
) -> None:
    """Continuously render, infer, validate and apply pi0.5 action chunks."""

    import numpy as np
    import rerun as rr
    from isaacsim.core.api.objects import DynamicCuboid, FixedCuboid
    from isaacsim.core.prims import RigidPrim
    from isaacsim.core.utils.types import ArticulationAction
    from isaacsim.core.utils.viewports import set_camera_view
    from isaacsim.core.utils.extensions import enable_extension

    # Isaac Sim 6 keeps the legacy Franka helper as an opt-in extension.
    enable_extension("isaacsim.robot.manipulators.examples")
    from isaacsim.robot.manipulators.examples.franka import Franka
    from isaacsim.sensors.camera import Camera

    world = antioch.world()
    world.scene.add_ground_plane(z_position=-0.75)
    tabletop = world.scene.add(
        FixedCuboid(
            prim_path="/World/Tabletop",
            name="tabletop",
            position=np.array([0.48, 0.0, -0.04]),
            scale=np.array([0.9, 0.7, 0.08]),
            color=np.array([0.55, 0.36, 0.2]),
        )
    )
    for index, (x, y) in enumerate(
        ((0.13, -0.25), (0.83, -0.25), (0.13, 0.25), (0.83, 0.25))
    ):
        world.scene.add(
            FixedCuboid(
                prim_path=f"/World/TableLeg{index}",
                name=f"table_leg_{index}",
                position=np.array([x, y, -0.39]),
                scale=np.array([0.07, 0.07, 0.7]),
                color=np.array([0.32, 0.22, 0.14]),
            )
        )
    robot = world.scene.add(Franka(prim_path="/World/Franka", name="franka"))
    cube = world.scene.add(
        DynamicCuboid(
            prim_path="/World/Cube",
            name="cube",
            position=np.array(CUBE_INITIAL_POSITION),
            size=CUBE_SIZE_METERS,
            color=np.array([0.95, 0.03, 0.02]),
        )
    )
    cube_gripper_contacts = world.scene.add(
        RigidPrim(
            prim_paths_expr="/World/Cube",
            name="cube_gripper_contacts",
            track_contact_forces=True,
            contact_filter_prim_paths_expr=[
                "/World/Franka/panda_leftfinger",
                "/World/Franka/panda_rightfinger",
            ],
            max_contact_count=16,
        )
    )
    exterior = Camera(
        prim_path="/World/PolicyExterior",
        position=np.array([1.15, -0.9, 0.72]),
        resolution=(224, 224),
    )
    wrist = Camera(
        prim_path="/World/Franka/panda_hand/PolicyWrist",
        resolution=(224, 224),
    )
    _configure_lighting(world.stage)
    world.reset()
    robot.set_joint_positions(
        np.asarray([*DROID_RESET_JOINTS, GRIPPER_JOINT_MAX, GRIPPER_JOINT_MAX])
    )
    exterior.initialize()
    wrist.initialize()
    _look_at(
        world.stage, "/World/PolicyExterior", [1.15, -0.9, 0.72], [0.46, 0.0, 0.08]
    )
    _look_at(
        world.stage,
        "/World/Franka/panda_hand/PolicyWrist",
        [0.055, 0.0, 0.045],
        [0.24, 0.0, -0.02],
    )
    set_camera_view(
        eye=[1.35, -1.15, 0.9],
        target=[0.44, 0.02, 0.1],
        camera_prim_path="/OmniverseKit_Persp",
    )
    overlay = _install_overlay()
    client = SafePolicyClient()
    observation_sequence = requests = round_trips = applied = safe_holds = 0
    camera_rejected_pairs = camera_validated_requests = 0
    rejected_actions: Counter[str] = Counter()
    transport_failures: Counter[str] = Counter()
    latencies_ms: list[float] = []
    luminance_means: list[float] = []
    luminance_variances: list[float] = []
    raw_gripper_range_mismatches = 0
    raw_joint_limit_mismatches = 0
    joint_limit_projections = 0
    joint_step_projections = 0
    last_latency = 0.0
    started = time.monotonic()
    next_attempt = 0.0
    chunk = None
    chunk_index = 0
    last_apply = time.monotonic()
    first_frame = True
    current_luminance_mean_min = 0.0
    current_luminance_variance_min = 0.0
    current_exterior_luminance_mean = 0.0
    current_exterior_luminance_variance = 0.0
    current_wrist_luminance_mean = 0.0
    current_wrist_luminance_variance = 0.0
    camera_pair_id = request_camera_pair_id = round_trip_camera_pair_id = 0
    pending = None
    pending_observation = 0
    pending_camera_pair_id = 0
    pending_joint_positions = None
    cube_initial_height = float(cube.get_world_pose()[0][2])
    physics_dt = float(world.get_physics_dt())
    initial_ee_distance = None
    minimum_ee_distance = float("inf")
    maximum_cube_lift = 0.0
    gripper_contact_samples = 0
    maximum_gripper_contact_force = 0.0
    pickup_hold_started = None
    pickup_hold_seconds = 0.0
    pickup_success = False
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
                    chunk, action_evidence = _validated_actions(
                        response, pending_joint_positions
                    )
                    raw_gripper_range_mismatches += action_evidence[
                        "raw_gripper_range_mismatches"
                    ]
                    raw_joint_limit_mismatches += action_evidence[
                        "raw_joint_limit_mismatches"
                    ]
                    joint_limit_projections += action_evidence[
                        "joint_limit_projections"
                    ]
                    joint_step_projections += action_evidence["joint_step_projections"]
                    chunk_index = 0
                    round_trips += 1
                    round_trip_camera_pair_id = pending_camera_pair_id
                    latencies_ms.append(last_latency * 1000.0)
                    percentiles = np.percentile(latencies_ms, [50, 95, 99])
                    print(
                        "NPA_OPENPI_ROUND_TRIP "
                        f"observation={pending_observation} "
                        f"round_trips={round_trips} "
                        f"latency_ms={last_latency * 1000.0:.3f} "
                        "action_shape=[15,8] finite=true safety_validated=true "
                        f"camera_pair_id={round_trip_camera_pair_id} "
                        f"raw_gripper_range_mismatches={raw_gripper_range_mismatches} "
                        f"raw_joint_limit_mismatches={raw_joint_limit_mismatches} "
                        f"joint_limit_projections={joint_limit_projections} "
                        f"joint_step_projections={joint_step_projections}",
                        flush=True,
                    )
                    print(
                        "NPA_OPENPI_METRICS "
                        f"elapsed_seconds={now - started:.3f} "
                        f"frames={observation_sequence} requests={requests} "
                        f"round_trips={round_trips} "
                        f"applied={applied} rejected_actions={sum(rejected_actions.values())} "
                        f"action_horizon={ACTION_SHAPE[0]} "
                        f"action_dimension={ACTION_SHAPE[1]} action_finite=1 "
                        f"rejected_wrong_shape={rejected_actions['wrong_shape']} "
                        f"rejected_non_finite={rejected_actions['non_finite']} "
                        f"rejected_joint_limit={rejected_actions['joint_limit']} "
                        f"rejected_gripper_range={rejected_actions['gripper_range']} "
                        f"rejected_joint_step={rejected_actions['joint_step']} "
                        f"raw_gripper_range_mismatches={raw_gripper_range_mismatches} "
                        f"raw_joint_limit_mismatches={raw_joint_limit_mismatches} "
                        f"joint_limit_projections={joint_limit_projections} "
                        f"joint_step_projections={joint_step_projections} "
                        f"transport_failures={sum(transport_failures.values())} "
                        f"reconnects={client.reconnects} "
                        "camera_quality_schema=2 "
                        f"camera_rejected_pairs={camera_rejected_pairs} "
                        f"camera_validated_requests={camera_validated_requests} "
                        f"camera_pair_id={camera_pair_id} "
                        f"request_camera_pair_id={request_camera_pair_id} "
                        f"round_trip_camera_pair_id={round_trip_camera_pair_id} "
                        f"camera_luminance_mean_current_min={current_luminance_mean_min:.3f} "
                        f"camera_luminance_variance_current_min={current_luminance_variance_min:.3f} "
                        f"camera_exterior_luminance_mean_current={current_exterior_luminance_mean:.3f} "
                        f"camera_exterior_luminance_variance_current={current_exterior_luminance_variance:.3f} "
                        f"camera_wrist_luminance_mean_current={current_wrist_luminance_mean:.3f} "
                        f"camera_wrist_luminance_variance_current={current_wrist_luminance_variance:.3f} "
                        f"luminance_mean_min={min(luminance_means):.3f} "
                        f"luminance_variance_min={min(luminance_variances):.3f} "
                        f"end_effector_cube_distance_m={minimum_ee_distance:.6f} "
                        f"end_effector_cube_approach_m={max(0.0, (initial_ee_distance or minimum_ee_distance) - minimum_ee_distance):.6f} "
                        f"gripper_contact_samples={gripper_contact_samples} "
                        f"gripper_contact_force_max_n={maximum_gripper_contact_force:.6f} "
                        f"cube_lift_max_m={maximum_cube_lift:.6f} "
                        f"pickup_hold_seconds={pickup_hold_seconds:.3f} "
                        f"pickup_success={int(pickup_success)} "
                        f"latency_p50_ms={percentiles[0]:.3f} "
                        f"latency_p95_ms={percentiles[1]:.3f} "
                        f"latency_p99_ms={percentiles[2]:.3f} "
                        f"latency_max_ms={max(latencies_ms):.3f}",
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
                        rejected_actions[reason] += 1
                        next_attempt = now + 1.0 / CONTROL_HZ
                    else:
                        transport_failures[reason] += 1
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
                    camera_rejected_pairs += 1
                    safe_holds += 1
                    next_attempt = now + 1.0 / CONTROL_HZ
                    overlay[2].text = "SAFE HOLD / waiting for camera frames"
                else:
                    if first_frame:
                        print("NPA_OPENPI_FIRST_FRAME", flush=True)
                        first_frame = False
                    observation_sequence += 1
                    exterior_luminance = np.mean(exterior_rgb, axis=2)
                    wrist_luminance = np.mean(wrist_rgb, axis=2)
                    luminance_means.extend(
                        [
                            float(exterior_luminance.mean()),
                            float(wrist_luminance.mean()),
                        ]
                    )
                    luminance_variances.extend(
                        [float(exterior_luminance.var()), float(wrist_luminance.var())]
                    )
                    current_exterior_luminance_mean = luminance_means[-2]
                    current_wrist_luminance_mean = luminance_means[-1]
                    current_exterior_luminance_variance = luminance_variances[-2]
                    current_wrist_luminance_variance = luminance_variances[-1]
                    current_luminance_mean_min = min(luminance_means[-2:])
                    current_luminance_variance_min = min(luminance_variances[-2:])
                    camera_pair_id += 1
                    observation = {
                        "observation/exterior_image_1_left": exterior_rgb,
                        "observation/wrist_image_left": wrist_rgb,
                        "observation/joint_position": joint_positions[:7],
                        "observation/gripper_position": np.asarray(
                            [_droid_gripper_observation(joint_positions)],
                            dtype=np.float32,
                        ),
                        "prompt": prompt,
                    }
                    requests += 1
                    camera_validated_requests += 1
                    request_camera_pair_id = camera_pair_id
                    print(
                        "NPA_OPENPI_REQUEST "
                        f"observation={observation_sequence} requests={requests} "
                        f"camera_pair_id={request_camera_pair_id} "
                        f"task_label={TASK_LABEL}",
                        flush=True,
                    )
                    logger.value("task/label", rr.TextLog(TASK_LABEL))
                    logger.image("camera/exterior", exterior_rgb)
                    logger.image("camera/wrist", wrist_rgb)
                    logger.scalar("camera/luminance_mean", luminance_means[-2])
                    logger.scalar("camera/luminance_variance", luminance_variances[-2])
                    pending_observation = observation_sequence
                    pending_camera_pair_id = request_camera_pair_id
                    pending_joint_positions = joint_positions.copy()
                    pending = executor.submit(client.infer, observation)

            if chunk is not None and now - last_apply >= 1.0 / CONTROL_HZ:
                target = chunk[chunk_index]
                robot.apply_action(
                    ArticulationAction(
                        joint_positions=np.concatenate(
                            [
                                target[:7],
                                np.repeat(_isaac_finger_target(target[7]), 2),
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

            cube_position = np.asarray(cube.get_world_pose()[0], dtype=np.float64)
            ee_position = np.asarray(
                robot.end_effector.get_world_pose()[0], dtype=np.float64
            )
            ee_distance = float(np.linalg.norm(ee_position - cube_position))
            if initial_ee_distance is None:
                initial_ee_distance = ee_distance
            minimum_ee_distance = min(minimum_ee_distance, ee_distance)
            cube_lift = max(0.0, float(cube_position[2]) - cube_initial_height)
            maximum_cube_lift = max(maximum_cube_lift, cube_lift)
            contact_force = _contact_force_magnitude(cube_gripper_contacts, physics_dt)
            maximum_gripper_contact_force = max(
                maximum_gripper_contact_force, contact_force
            )
            in_gripper_contact = contact_force >= GRIPPER_CONTACT_FORCE_NEWTONS
            if in_gripper_contact:
                gripper_contact_samples += 1
            current_joint_positions = np.asarray(
                robot.get_joint_positions(), dtype=float
            )
            gripper_closed = _droid_gripper_observation(current_joint_positions) > 0.5
            pickup_candidate = bool(
                cube_lift >= PICKUP_LIFT_METERS
                and in_gripper_contact
                and gripper_closed
            )
            if pickup_candidate:
                if pickup_hold_started is None:
                    pickup_hold_started = now
                pickup_hold_seconds = now - pickup_hold_started
                pickup_success = pickup_hold_seconds >= PICKUP_HOLD_SECONDS
            else:
                pickup_hold_started = None
                if not pickup_success:
                    pickup_hold_seconds = 0.0

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
            logger.scalar(
                "decision/raw_gripper_range_mismatches",
                raw_gripper_range_mismatches,
            )
            logger.scalar("decision/rejected_actions", sum(rejected_actions.values()))
            logger.scalar(
                "decision/raw_joint_limit_mismatches",
                raw_joint_limit_mismatches,
            )
            logger.scalar("decision/joint_limit_projections", joint_limit_projections)
            logger.scalar("decision/joint_step_projections", joint_step_projections)
            logger.scalar("grasp/end_effector_cube_distance_m", ee_distance)
            logger.scalar(
                "grasp/end_effector_cube_approach_m",
                max(0.0, initial_ee_distance - minimum_ee_distance),
            )
            logger.scalar("grasp/gripper_contact_force_n", contact_force)
            logger.scalar("grasp/gripper_contact", int(in_gripper_contact))
            logger.scalar("grasp/gripper_closed", int(gripper_closed))
            logger.scalar("grasp/cube_lift_m", cube_lift)
            logger.scalar("grasp/pickup_hold_seconds", pickup_hold_seconds)
            logger.scalar("grasp/pickup_success", int(pickup_success))
            logger.scalar(
                "decision/applied_target_rate_hz",
                applied / max(now - started, 1e-6),
            )
            logger.value(
                "scene/cube",
                rr.Boxes3D(
                    centers=[cube_position.tolist()],
                    sizes=[[CUBE_SIZE_METERS] * 3],
                    colors=[[242, 8, 5, 255]],
                ),
            )
            logger.value(
                "scene/table",
                rr.Boxes3D(
                    centers=[tabletop.get_world_pose()[0].tolist()],
                    sizes=[[0.9, 0.7, 0.08]],
                    colors=[[140, 92, 51, 255]],
                ),
            )
            for index, value in enumerate(current_joint_positions[:9]):
                logger.scalar(f"robot/franka/joint_{index}", float(value))
            link_points = _franka_link_points(world.stage)
            proxy = _franka_proxy_geometry(link_points)
            if proxy["base"] is not None:
                logger.value(
                    "scene/franka/base",
                    rr.Boxes3D(**proxy["base"]),
                )
            if proxy["links"]["centers"]:
                logger.value("scene/franka/links", rr.Boxes3D(**proxy["links"]))
            if proxy["joints"] is not None:
                logger.value(
                    "scene/franka/joints",
                    rr.Ellipsoids3D(**proxy["joints"]),
                )
            if proxy["gripper"] is not None:
                logger.value(
                    "scene/franka/gripper",
                    rr.Boxes3D(**proxy["gripper"]),
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
        run.add_result("raw_gripper_range_mismatches", raw_gripper_range_mismatches)
        run.add_result("raw_joint_limit_mismatches", raw_joint_limit_mismatches)
        run.add_result("joint_limit_projections", joint_limit_projections)
        run.add_result("joint_step_projections", joint_step_projections)
        run.add_result("reconnects", client.reconnects)
        run.add_result("rejected_actions", dict(sorted(rejected_actions.items())))
        run.add_result("transport_failures", dict(sorted(transport_failures.items())))
        run.add_result("camera_quality_schema", 2)
        run.add_result("camera_rejected_pairs", camera_rejected_pairs)
        run.add_result("camera_validated_requests", camera_validated_requests)
        run.add_result("camera_pair_id", camera_pair_id)
        run.add_result("request_camera_pair_id", request_camera_pair_id)
        run.add_result("round_trip_camera_pair_id", round_trip_camera_pair_id)
        run.add_result("task_label", TASK_LABEL)
        run.add_result("minimum_end_effector_cube_distance_m", minimum_ee_distance)
        run.add_result("maximum_cube_lift_m", maximum_cube_lift)
        run.add_result("gripper_contact_samples", gripper_contact_samples)
        run.add_result("maximum_gripper_contact_force_n", maximum_gripper_contact_force)
        run.add_result("pickup_hold_seconds", pickup_hold_seconds)
        run.add_result("pickup_success", pickup_success)
        if latencies_ms:
            percentiles = np.percentile(latencies_ms, [50, 95, 99])
            run.add_result("latency_p50_ms", float(percentiles[0]))
            run.add_result("latency_p95_ms", float(percentiles[1]))
            run.add_result("latency_p99_ms", float(percentiles[2]))
            run.add_result("latency_max_ms", float(max(latencies_ms)))
        if luminance_means:
            run.add_result("camera_luminance_mean_min", float(min(luminance_means)))
            run.add_result(
                "camera_luminance_variance_min", float(min(luminance_variances))
            )
