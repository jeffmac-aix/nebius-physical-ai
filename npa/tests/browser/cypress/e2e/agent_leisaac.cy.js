describe("NPA agent LeIsaac capability tab", () => {
  beforeEach(() => {
    cy.visitMockAgent();
    cy.wait("@session");
  });

  it("is absent when the selected run has no usable live capability", () => {
    cy.get("#tabLeIsaac").should("not.exist");
    cy.get("#panelLeIsaac").should("not.exist");
  });

  it("shows task/environment metadata and enforces recorder transitions", () => {
    let recorderState = "idle";
    let pendingOutcome = "";
    let completed = 1;
    let lastOutcome = "failure";
    let lastCommandId = "";
    let lastCommand = "";
    const statusBody = () => ({
      available: true,
      run_id: "mock-recorder",
      task: "LeIsaac-SO101-LiftCube-v0",
      environment_id: "table-b",
      environment_index: 1,
      seed: 43,
      dataset_uri: "s3://bucket/datasets/leisaac",
      stream_transport: "jpeg-poll",
      frame_url: "/api/leisaac/frame.jpg?run_id=mock-recorder",
      input_url: "/api/leisaac/input?run_id=mock-recorder",
      recorder_url: "/api/leisaac/recorder?run_id=mock-recorder",
      recorder: {
        state: recorderState,
        active_episode: recorderState === "idle" ? null : "episode-uuid",
        frame_count: recorderState === "idle" ? 0 : 7,
        completed_episode_count: completed,
        pending_outcome: pendingOutcome,
        last_outcome: lastOutcome,
        last_upload_status: completed > 1 ? "uploaded" : "never",
        last_error: "",
        last_command_id: lastCommandId,
        last_command: lastCommand,
        command_revision: completed,
        dataset_version_uri:
          completed > 1 ? "s3://bucket/datasets/leisaac/versions/test" : "",
        last_episode_commit_uri:
          completed > 1
            ? "s3://bucket/datasets/leisaac/commits/latest.json"
            : "",
      },
      gpu: "NVIDIA RTX PRO 6000 Blackwell Server Edition",
    });
    cy.intercept("GET", "/api/leisaac/status?run_id=mock-recorder", (req) =>
      req.reply(statusBody()),
    ).as("recorderStatus");
    cy.intercept("POST", "/api/leisaac/select", {
      statusCode: 200,
      body: { selected: true },
    });
    cy.intercept(
      "POST",
      "/api/leisaac/recorder?run_id=mock-recorder",
      (req) => {
        expect(req.headers["x-npa-leisaac-control"]).to.equal("1");
        expect(req.body.request_id).to.be.a("string").and.not.be.empty;
        if (req.body.command === "start" && recorderState === "idle")
          recorderState = "recording";
        else if (
          req.body.command === "mark-success" &&
          recorderState === "recording"
        ) {
          recorderState = "outcome-pending";
          pendingOutcome = "success";
        } else if (
          req.body.command === "mark-failure" &&
          recorderState === "recording"
        ) {
          recorderState = "outcome-pending";
          pendingOutcome = "failure";
        } else if (
          req.body.command === "finalize" &&
          recorderState === "outcome-pending"
        ) {
          recorderState = "idle";
          lastOutcome = pendingOutcome;
          pendingOutcome = "";
          completed += 1;
        } else {
          req.reply({
            statusCode: 409,
            body: { detail: "invalid transition" },
          });
          return;
        }
        lastCommandId = req.body.request_id;
        lastCommand = req.body.command;
        req.reply({ statusCode: 202, body: { accepted: true } });
      },
    ).as("recorderControl");

    cy.window().then((win) =>
      win.__NPA_AGENT_TEST__.refreshLeIsaacCapability("mock-recorder"),
    );
    cy.wait("@recorderStatus");
    cy.get("#tabLeIsaac").click();
    cy.get("#panelLeIsaac").should("contain.text", "LeIsaac-SO101-LiftCube-v0");
    cy.get("#panelLeIsaac").should("contain.text", "table-b [1]");
    cy.get("#panelLeIsaac").should(
      "contain.text",
      "s3://bucket/datasets/leisaac",
    );
    cy.get("#leisaacRecordStart").should("not.be.disabled");
    cy.get("#leisaacRecordSuccess")
      .should("be.disabled")
      .and("have.attr", "title")
      .and("contain", "Start an episode");
    cy.get("#leisaacRecordFinalize").should("be.disabled");
    cy.get("#leisaacRecorderGuidance").should(
      "contain.text",
      "Start an episode",
    );
    cy.get("#leisaacRecordStart").then(($button) => {
      $button[0].click();
      $button[0].click();
    });
    cy.wait("@recorderControl");
    cy.get("@recorderControl.all").should("have.length", 1);
    cy.get("#leisaacRecorderStatus").should("contain.text", "State: recording");
    cy.get("#leisaacRecordSuccess").should("not.be.disabled").click();
    cy.wait("@recorderControl");
    cy.get("#leisaacRecorderGuidance").should(
      "contain.text",
      "Outcome selected: success",
    );
    cy.get("#leisaacRecordFinalize").should("not.be.disabled").click();
    cy.wait("@recorderControl");
    cy.get("#leisaacRecorderStatus").should("contain.text", "completed: 2");
    cy.get("#leisaacRecorderArtifact").should(
      "contain.text",
      "Immutable dataset",
    );

    cy.get("#leisaacRecordStart").should("not.be.disabled").click();
    cy.wait("@recorderControl");
    cy.get("#leisaacRecordFailure").should("not.be.disabled").click();
    cy.wait("@recorderControl");
    cy.get("#leisaacRecorderGuidance").should(
      "contain.text",
      "Outcome selected: failure",
    );
    cy.get("#leisaacRecordFinalize").should("not.be.disabled").click();
    cy.wait("@recorderControl");
    cy.get("#leisaacRecorderStatus")
      .should("contain.text", "State: idle")
      .and("contain.text", "completed: 3")
      .and("contain.text", "failure/uploaded");
    cy.screenshot("leisaac-recorder-transition");
  });

  it("surfaces a failed upload and retries the same episode", () => {
    let recorderState = "outcome-pending";
    let attempts = 0;
    let lastCommandId = "";
    cy.intercept("GET", "/api/leisaac/status?run_id=mock-retry", (req) => {
      req.reply({
        available: true,
        run_id: "mock-retry",
        task: "LeIsaac-SO101-PickOrange-v0",
        environment_id: "counter-a",
        environment_index: 0,
        seed: 42,
        dataset_uri: "s3://bucket/datasets/retry",
        recorder_url: "/api/leisaac/recorder?run_id=mock-retry",
        recorder: {
          state: recorderState,
          active_episode: recorderState === "idle" ? null : "retry-episode",
          frame_count: recorderState === "idle" ? 0 : 8,
          completed_episode_count: recorderState === "idle" ? 1 : 0,
          pending_outcome: recorderState === "idle" ? "" : "failure",
          last_outcome: recorderState === "idle" ? "failure" : "",
          last_upload_status:
            recorderState === "upload-failed"
              ? "failed"
              : recorderState === "idle"
                ? "uploaded"
                : "recording",
          last_error:
            recorderState === "upload-failed"
              ? "temporary object-store failure"
              : "",
          last_command_id: lastCommandId,
          last_command: lastCommandId ? "finalize" : "",
          dataset_version_uri:
            recorderState === "idle"
              ? "s3://bucket/datasets/retry/versions/v1"
              : "",
        },
      });
    }).as("retryStatus");
    cy.intercept("POST", "/api/leisaac/select", {
      statusCode: 200,
      body: { selected: true },
    });
    cy.intercept("POST", "/api/leisaac/recorder?run_id=mock-retry", (req) => {
      attempts += 1;
      lastCommandId = req.body.request_id;
      if (attempts === 1) {
        recorderState = "upload-failed";
        req.reply({
          statusCode: 202,
          body: { accepted: true, request_id: lastCommandId },
        });
      } else {
        recorderState = "idle";
        req.reply({
          statusCode: 202,
          body: { accepted: true, request_id: lastCommandId },
        });
      }
    }).as("retryControl");

    cy.window().then((win) =>
      win.__NPA_AGENT_TEST__.refreshLeIsaacCapability("mock-retry"),
    );
    cy.wait("@retryStatus");
    cy.get("#tabLeIsaac").click();
    cy.get("#leisaacRecordFinalize").should("not.be.disabled").click();
    cy.wait("@retryControl");
    cy.get("#leisaacRecorderError").should(
      "contain.text",
      "temporary object-store failure",
    );
    cy.window().then((win) => win.__NPA_AGENT_TEST__.pollLeIsaacRecorder());
    cy.get("#leisaacRecorderStatus").should(
      "contain.text",
      "State: upload-failed",
    );
    cy.get("#leisaacRecordFinalize")
      .should("not.be.disabled")
      .and("have.attr", "title")
      .and("contain", "Retry");
    cy.get("#leisaacRecordFinalize").click();
    cy.wait("@retryControl");
    cy.get("#leisaacRecorderStatus")
      .should("contain.text", "State: idle")
      .and("contain.text", "completed: 1");
  });

  it("unlocks controls after a rejected recorder request and allows retry", () => {
    let recorderState = "idle";
    let lastCommandId = "";
    const status = () => ({
      available: true,
      run_id: "mock-network-retry",
      task: "LeIsaac-SO101-PickOrange-v0",
      environment_id: "counter-a",
      environment_index: 0,
      seed: 42,
      recorder_url: "/api/leisaac/recorder?run_id=mock-network-retry",
      recorder: {
        state: recorderState,
        active_episode: recorderState === "recording" ? "episode-retry" : null,
        frame_count: recorderState === "recording" ? 3 : 0,
        completed_episode_count: 0,
        pending_outcome: "",
        last_outcome: "",
        last_upload_status:
          recorderState === "recording" ? "recording" : "never",
        last_error: "",
        last_command_id: lastCommandId,
        last_command: lastCommandId ? "start" : "",
      },
    });
    cy.intercept("GET", "/api/leisaac/status*", (req) => {
      req.reply(status());
    }).as("networkRetryStatus");
    cy.intercept("POST", "/api/leisaac/select", {
      statusCode: 200,
      body: { selected: true },
    });
    cy.intercept(
      "POST",
      "/api/leisaac/recorder?run_id=mock-network-retry",
      (req) => {
        recorderState = "recording";
        lastCommandId = req.body.request_id;
        req.reply({
          statusCode: 202,
          body: { accepted: true, request_id: lastCommandId },
        });
      },
    ).as("networkRetryControl");

    cy.window().then((win) => {
      win.__NPA_AGENT_TEST__.selectActiveRunId("mock-network-retry");
      return win.__NPA_AGENT_TEST__.refreshLeIsaacCapability(
        "mock-network-retry",
      );
    });
    cy.wait("@networkRetryStatus");
    cy.get("#tabLeIsaac").click();
    cy.window().then((win) => {
      const originalFetch = win.fetch.bind(win);
      let rejectRecorderRequest = true;
      win.fetch = (url, options) => {
        if (
          rejectRecorderRequest &&
          String(url).includes("/api/leisaac/recorder?") &&
          options &&
          options.method === "POST"
        ) {
          rejectRecorderRequest = false;
          return Promise.reject(new TypeError("simulated network disconnect"));
        }
        return originalFetch(url, options);
      };
    });
    cy.get("#leisaacRecordStart").should("not.be.disabled").click();
    cy.get("#leisaacRecorderError")
      .should("contain.text", "Recorder command failed")
      .and("contain.text", "retry is available");
    cy.get("#leisaacRecordStart").should("not.be.disabled").click();
    cy.wait("@networkRetryControl");
    cy.get("#leisaacRecorderStatus").should("contain.text", "State: recording");
    cy.get("#leisaacRecordSuccess").should("not.be.disabled");
  });

  it("appears only for a live run and drives the upstream keyboard client", () => {
    cy.intercept("GET", "/api/leisaac/status?run_id=mock-run", {
      statusCode: 200,
      body: {
        available: true,
        run_id: "mock-run",
        transport: "agent-relay",
        task: "LeIsaac-SO101-PickOrange-v0",
        teleop_device: "keyboard",
        media_server: "203.0.113.50",
        media_port: 47998,
        ice_transport_policy: "relay",
        ice_servers: [
          {
            urls: ["turn:203.0.113.50:3478?transport=udp"],
            username: "mock-run",
            credential: "ephemeral-test-credential",
          },
        ],
        signaling_server: "same-origin",
        signaling_port: 443,
        signaling_path: "/api/leisaac/signal",
        client_module_url: "/api/leisaac/client/index.js?run_id=mock-run",
        source_version: "0.4.0",
        source_commit: "1651c321e9b0c1bb54233211fc7b3cd70d8373d5",
        isaac_sim_version: ["5", "1", "0", "0"].join("."),
        isaac_lab_version: "2.3.2.post1",
        image: "registry/npa-leisaac@sha256:test",
        gpu: "NVIDIA RTX PRO 6000 Blackwell Server Edition",
      },
    }).as("leisaacStatus");
    cy.intercept("GET", "/api/leisaac/client/index.js?run_id=mock-run", {
      statusCode: 200,
      headers: { "content-type": "text/javascript" },
      body: `window.OVWebStreamingLibrary = { AppStreamer: {
        connect: async function(props) {
          window.__LEISAAC_CONNECT_PROPS__ = props;
          new window.RTCPeerConnection({ iceServers: [{ urls: "stun:untrusted.invalid" }] });
          props.streamConfig.onStart({ status: "success" });
          return { status: "inProgress" };
        },
        terminate: async function() { window.__LEISAAC_TERMINATED__ = true; }
      }};`,
    }).as("leisaacClient");
    cy.intercept("POST", "/api/leisaac/select", {
      statusCode: 200,
      body: { selected: true, run_id: "mock-run", available: true },
    }).as("leisaacSelect");

    cy.window().then((win) =>
      win.__NPA_AGENT_TEST__.refreshLeIsaacCapability("mock-run"),
    );
    cy.wait("@leisaacStatus");
    cy.wait("@leisaacSelect");
    cy.get("#tabLeIsaac").should("exist").click();
    cy.get("#panelLeIsaac").should("have.class", "is-active");
    cy.intercept("GET", "/api/sim-viz/status?run_id=older-rerun-only-run", {
      statusCode: 200,
      delay: 200,
      body: {
        run_id: "older-rerun-only-run",
        active_run_id: "older-rerun-only-run",
        stage: "artifacts_available",
        available_runs: [],
      },
    }).as("selectedRunRefresh");
    cy.intercept("GET", "/api/leisaac/status?run_id=older-rerun-only-run", {
      statusCode: 200,
      body: { available: false, reason: "selected run is not LeIsaac" },
    }).as("unrelatedLeisaacStatus");
    cy.intercept("GET", "/api/leisaac/status", {
      statusCode: 200,
      body: {
        available: true,
        run_id: "mock-run",
        transport: "agent-relay",
        task: "LeIsaac-SO101-PickOrange-v0",
        teleop_device: "keyboard",
        media_server: "203.0.113.50",
        media_port: 47998,
        ice_transport_policy: "relay",
        ice_servers: [
          {
            urls: ["turn:203.0.113.50:3478?transport=udp"],
            username: "mock-run",
            credential: "ephemeral-test-credential",
          },
        ],
        signaling_path: "/api/leisaac/signal",
        client_module_url: "/api/leisaac/client/index.js?run_id=mock-run",
        gpu: "NVIDIA RTX PRO 6000 Blackwell Server Edition",
      },
    }).as("rememberedLeisaacStatus");
    cy.window().then((win) => {
      win.__NPA_AGENT_TEST__.selectActiveRunId("older-rerun-only-run");
      const pendingRefresh = win.__NPA_AGENT_TEST__.refresh();
      win.__NPA_AGENT_TEST__.selectActiveRunId("mock-run");
      return pendingRefresh;
    });
    cy.wait("@selectedRunRefresh");
    cy.window().then((win) => {
      expect(win.__NPA_AGENT_TEST__.activeRunId()).to.equal("mock-run");
    });
    cy.get("#tabLeIsaac").should("exist");
    cy.window().then((win) =>
      win.__NPA_AGENT_TEST__.refreshLeIsaacCapability("older-rerun-only-run"),
    );
    cy.wait("@unrelatedLeisaacStatus");
    cy.wait("@rememberedLeisaacStatus");
    cy.get("#tabLeIsaac").should("exist");
    cy.window().then((win) => {
      function CapturingPeerConnection(config) {
        win.__LEISAAC_PEER_CONFIG__ = config;
      }
      CapturingPeerConnection.prototype = {};
      win.__LEISAAC_NATIVE_PEER__ = CapturingPeerConnection;
      win.RTCPeerConnection = CapturingPeerConnection;
    });
    cy.get("#leisaacConnect").click();
    cy.wait("@leisaacClient");
    cy.get("#leisaacStreamStatus").should(
      "contain.text",
      "keyboard teleoperation active",
    );
    cy.window()
      .its("__LEISAAC_CONNECT_PROPS__.streamConfig.signalingPath")
      .should("eq", "/api/leisaac/signal");
    cy.window()
      .its("__LEISAAC_CONNECT_PROPS__.streamConfig.forceWSS")
      .should("eq", true);
    cy.window()
      .its("__LEISAAC_CONNECT_PROPS__.streamConfig.mediaPort")
      .should("eq", 47998);
    cy.window()
      .its("__LEISAAC_CONNECT_PROPS__.streamConfig.width")
      .should("eq", 1920);
    cy.window()
      .its("__LEISAAC_CONNECT_PROPS__.streamConfig.height")
      .should("eq", 1080);
    cy.window()
      .its("__LEISAAC_CONNECT_PROPS__.streamConfig.fps")
      .should("eq", 60);
    cy.window()
      .its("__LEISAAC_PEER_CONFIG__.iceTransportPolicy")
      .should("eq", "relay");
    cy.window()
      .its("__LEISAAC_PEER_CONFIG__.iceServers.0.urls.0")
      .should("eq", "turn:203.0.113.50:3478?transport=udp");
    cy.get("#leisaacStreamHost").trigger("keydown", { key: "W", code: "KeyW" });
    cy.get("#leisaacInputStatus")
      .should("contain.text", "Keyboard events sent: 1")
      .and("contain.text", "last W");

    cy.intercept("GET", "/api/leisaac/status?run_id=mock-run", {
      statusCode: 200,
      body: { available: false, reason: "session expired" },
    }).as("leisaacGone");
    cy.intercept("GET", "/api/leisaac/status", {
      statusCode: 200,
      body: { available: false, reason: "session expired" },
    }).as("rememberedLeisaacGone");
    cy.window().then((win) =>
      win.__NPA_AGENT_TEST__.refreshLeIsaacCapability("mock-run"),
    );
    cy.wait("@leisaacGone");
    cy.wait("@rememberedLeisaacGone");
    cy.get("#tabLeIsaac").should("not.exist");
    cy.get("#panelLeIsaac").should("not.exist");
    cy.window().then((win) =>
      expect(win.RTCPeerConnection).to.equal(win.__LEISAAC_NATIVE_PEER__),
    );
  });

  it("polls authenticated RTX frames and forwards keyboard controls", () => {
    cy.intercept("GET", "/api/leisaac/status?run_id=mock-jpeg", {
      statusCode: 200,
      body: {
        available: true,
        run_id: "mock-jpeg",
        task: "LeIsaac-SO101-PickOrange-v0",
        teleop_device: "keyboard",
        stream_transport: "jpeg-poll",
        frame_url: "/api/leisaac/frame.jpg?run_id=mock-jpeg",
        input_url: "/api/leisaac/input?run_id=mock-jpeg",
        gpu: "NVIDIA RTX PRO 6000 Blackwell Server Edition",
      },
    }).as("jpegStatus");
    cy.intercept("GET", "/api/leisaac/frame.jpg?run_id=mock-jpeg&frame=*", {
      statusCode: 200,
      headers: { "content-type": "image/svg+xml", "cache-control": "no-store" },
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450"><rect width="800" height="450" fill="#2563eb"/></svg>',
    }).as("jpegFrame");
    cy.intercept("POST", "/api/leisaac/input?run_id=mock-jpeg", {
      statusCode: 202,
      body: { accepted: true },
    }).as("jpegInput");
    cy.intercept("POST", "/api/leisaac/select", {
      statusCode: 200,
      body: { selected: true, run_id: "mock-jpeg", available: true },
    }).as("jpegSelect");

    cy.window().then((win) =>
      win.__NPA_AGENT_TEST__.refreshLeIsaacCapability("mock-jpeg"),
    );
    cy.wait("@jpegStatus");
    cy.wait("@jpegSelect");
    cy.get("#tabLeIsaac").click();
    cy.get("#leisaacConnect").click();
    cy.wait("@jpegFrame");
    cy.get("#leisaacFrame")
      .should("be.visible")
      .and(($frame) => expect($frame[0].naturalWidth).to.equal(800));
    cy.get("#leisaacStreamStatus").should(
      "contain.text",
      "keyboard teleoperation active",
    );
    cy.get("#leisaacStreamHost")
      .click()
      .trigger("keydown", { key: "W", code: "KeyW" });
    cy.wait("@jpegInput").then(({ request }) => {
      expect(request.headers["x-npa-leisaac-control"]).to.equal("1");
      expect(request.body).to.include({
        v: 1,
        type: "control",
        run_id: "mock-jpeg",
        key: "W",
        event: "press",
      });
      expect(request.body.seq).to.equal(1);
      expect(request.body.client_id).to.match(/^browser-/);
    });
    cy.get("#leisaacInputStatus").should(
      "contain.text",
      "Keyboard events sent: 1",
    );
    cy.get("#leisaacDisconnect").click();
  });

  it("uses preferred binary WebSockets while controls and recorder stay responsive", () => {
    let recorderState = "idle";
    const status = () => ({
      available: true,
      run_id: "mock-websocket",
      task: "LeIsaac-SO101-LiftCube-v0",
      environment_id: "latency-test",
      environment_index: 2,
      seed: 47,
      stream_transport: "websocket-v1",
      preferred_transport: "websocket-v1",
      control_ws_url: "/api/leisaac/transport/control?run_id=mock-websocket",
      video_ws_url: "/api/leisaac/transport/video?run_id=mock-websocket",
      frame_url: "/api/leisaac/frame.jpg?run_id=mock-websocket",
      input_url: "/api/leisaac/input?run_id=mock-websocket",
      recorder_url: "/api/leisaac/recorder?run_id=mock-websocket",
      gpu: "NVIDIA RTX PRO 6000 Blackwell Server Edition",
      recorder: {
        state: recorderState,
        active_episode: recorderState === "recording" ? "episode-live" : null,
        frame_count: recorderState === "recording" ? 12 : 0,
        completed_episode_count: 1,
        pending_outcome: "",
        last_outcome: "success",
        last_upload_status: "uploaded",
        last_error: "",
      },
    });
    cy.intercept("GET", "/api/leisaac/status?run_id=mock-websocket", (req) =>
      req.reply(status()),
    ).as("wsStatus");
    cy.intercept("GET", "/api/leisaac/status?run_id=mock-run", (req) =>
      req.reply(status()),
    );
    cy.intercept("GET", "/api/leisaac/status", (req) => req.reply(status()));
    cy.intercept("POST", "/api/leisaac/select", {
      statusCode: 200,
      body: { selected: true, run_id: "mock-websocket", available: true },
    });
    cy.intercept(
      "POST",
      "/api/leisaac/recorder?run_id=mock-websocket",
      (req) => {
        recorderState = req.body.command === "start" ? "recording" : recorderState;
        req.reply({ statusCode: 202, body: { accepted: true } });
      },
    ).as("wsRecorder");

    cy.window().then((win) => {
      let nextExpected = 1;
      let frameSequence = 0;
      const sockets = [];
      const encodeFrame = () => {
        const jpeg = new win.Uint8Array([0xff, 0xd8, 1, 2, 3, 4, 0xff, 0xd9]);
        const payload = new win.ArrayBuffer(112 + jpeg.length);
        const view = new win.DataView(payload);
        [0x4e, 0x50, 0x41, 0x46].forEach((value, index) =>
          view.setUint8(index, value),
        );
        view.setUint8(4, 1);
        view.setUint16(6, 112, false);
        frameSequence += 1;
        view.setBigUint64(8, BigInt(frameSequence), false);
        view.setBigUint64(16, BigInt(win.Date.now()) * 1000000n, false);
        view.setBigUint64(24, BigInt(Math.floor(win.performance.now() * 1000000)), false);
        view.setBigUint64(32, BigInt(win.Date.now()) * 1000000n, false);
        view.setBigUint64(40, BigInt(Math.floor(win.performance.now() * 1000000)), false);
        view.setBigUint64(48, 500n, false);
        view.setBigUint64(56, 600n, false);
        view.setBigUint64(64, 700n, false);
        view.setUint32(72, jpeg.length, false);
        view.setUint32(76, Math.max(0, frameSequence - 1), false);
        new win.Uint8Array(payload, 112).set(jpeg);
        return payload;
      };
      class FakeWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSING = 2;
        static CLOSED = 3;
        constructor(url, protocol) {
          this.url = String(url);
          this.protocol = protocol;
          this.readyState = FakeWebSocket.CONNECTING;
          this.binaryType = "blob";
          this.timer = null;
          sockets.push(this);
          win.setTimeout(() => {
            this.readyState = FakeWebSocket.OPEN;
            if (this.onopen) this.onopen({ target: this });
            if (this.url.includes("/video")) {
              this.timer = win.setInterval(() => {
                if (this.onmessage) this.onmessage({ data: encodeFrame() });
              }, 12);
            }
          }, 5);
        }
        send(raw) {
          if (!this.url.includes("/control")) return;
          const message = JSON.parse(String(raw));
          let response = null;
          if (message.type === "resume") {
            response = {
              v: 1,
              type: "resumed",
              run_id: message.run_id,
              client_id: message.client_id,
              next_seq: nextExpected,
              last_applied_seq: nextExpected - 1,
              keys_down: [],
            };
          } else if (message.type === "ping") {
            response = {
              ...message,
              type: "pong",
              runtime_wall_ns: String(BigInt(win.Date.now()) * 1000000n),
            };
          } else if (message.type === "control") {
            expect(message.seq).to.equal(nextExpected);
            nextExpected += 1;
            response = { ...message, type: "ack", phase: "accepted" };
            win.setTimeout(() => {
              if (this.onmessage)
                this.onmessage({
                  data: JSON.stringify({
                    ...message,
                    type: "ack",
                    phase: "applied",
                    simulator_applied_mono_ns: "800",
                    simulator_step: message.seq,
                  }),
                });
            }, 2);
          }
          if (response)
            win.setTimeout(
              () => this.onmessage && this.onmessage({ data: JSON.stringify(response) }),
              0,
            );
        }
        close() {
          if (this.timer) win.clearInterval(this.timer);
          this.readyState = FakeWebSocket.CLOSED;
        }
        fail() {
          if (this.timer) win.clearInterval(this.timer);
          this.readyState = FakeWebSocket.CLOSED;
          if (this.onclose) this.onclose({ code: 1012 });
        }
      }
      win.WebSocket = FakeWebSocket;
      win.createImageBitmap = async () => ({ width: 1280, height: 720, close() {} });
      const nativeGetContext = win.HTMLCanvasElement.prototype.getContext;
      win.HTMLCanvasElement.prototype.getContext = function getContext(kind, ...args) {
        if (this.id === "leisaacCanvas" && kind === "2d") return { drawImage() {} };
        return nativeGetContext.call(this, kind, ...args);
      };
      win.__LEISAAC_FAKE_SOCKETS__ = sockets;
    });

    cy.window().then((win) =>
      win.__NPA_AGENT_TEST__.refreshLeIsaacCapability("mock-websocket"),
    );
    cy.wait("@wsStatus");
    cy.get("#tabLeIsaac").click();
    cy.get("#leisaacConnect").click();
    cy.get("#leisaacTransportStatus", { timeout: 10000 })
      .should("contain.text", "WebSocket")
      .and("contain.text", "latest-frame-wins");
    cy.get("#leisaacCanvas").should("be.visible");
    cy.get("#leisaacStreamStatus").should(
      "contain.text",
      "keyboard teleoperation active",
    );
    cy.window().then((win) => {
      const host = win.document.getElementById("leisaacStreamHost");
      host.focus();
      host.dispatchEvent(
        new win.KeyboardEvent("keydown", { key: "W", code: "KeyW", bubbles: true }),
      );
      host.dispatchEvent(
        new win.KeyboardEvent("keyup", { key: "W", code: "KeyW", bubbles: true }),
      );
    });
    cy.window().should((win) => {
      const controls = win.__NPA_AGENT_TEST__.leisaacTransportEvidence().controls;
        expect(controls.filter((item) => item.phase === "accepted")).to.have.length(2);
        expect(controls.filter((item) => item.phase === "applied")).to.have.length(2);
    });
    cy.get("#leisaacRecordStart").click();
    cy.wait("@wsRecorder");
    cy.get("#leisaacRecorderStatus").should("contain.text", "recording");

    cy.window().then((win) => {
      const video = win.__LEISAAC_FAKE_SOCKETS__.find(
        (socket) => socket.url.includes("/video") && socket.readyState === 1,
      );
      expect(video).to.exist;
      video.fail();
    });
    cy.get("#leisaacTransportStatus", { timeout: 10000 }).should(
      "contain.text",
      "WebSocket",
    );
    cy.window()
      .then((win) => win.__NPA_AGENT_TEST__.leisaacTransportEvidence())
      .its("reconnects")
      .should("be.gte", 0);
    cy.get("#leisaacDisconnect").click();
  });

  it("falls back explicitly after bounded preferred-transport retries", () => {
    cy.intercept("GET", "/api/leisaac/status?run_id=mock-ws-fallback", {
      statusCode: 200,
      body: {
        available: true,
        run_id: "mock-ws-fallback",
        task: "LeIsaac-SO101-PickOrange-v0",
        stream_transport: "websocket-v1",
        control_ws_url: "/api/leisaac/transport/control?run_id=mock-ws-fallback",
        video_ws_url: "/api/leisaac/transport/video?run_id=mock-ws-fallback",
        frame_url: "/api/leisaac/frame.jpg?run_id=mock-ws-fallback",
        input_url: "/api/leisaac/input?run_id=mock-ws-fallback",
      },
    }).as("wsFallbackStatus");
    cy.intercept("GET", "/api/leisaac/status?run_id=mock-run", {
      statusCode: 200,
      body: {
        available: true,
        run_id: "mock-ws-fallback",
        task: "LeIsaac-SO101-PickOrange-v0",
        stream_transport: "websocket-v1",
        control_ws_url: "/api/leisaac/transport/control?run_id=mock-ws-fallback",
        video_ws_url: "/api/leisaac/transport/video?run_id=mock-ws-fallback",
        frame_url: "/api/leisaac/frame.jpg?run_id=mock-ws-fallback",
        input_url: "/api/leisaac/input?run_id=mock-ws-fallback",
      },
    });
    cy.intercept("GET", "/api/leisaac/status", {
      statusCode: 200,
      body: {
        available: true,
        run_id: "mock-ws-fallback",
        task: "LeIsaac-SO101-PickOrange-v0",
        stream_transport: "websocket-v1",
        control_ws_url: "/api/leisaac/transport/control?run_id=mock-ws-fallback",
        video_ws_url: "/api/leisaac/transport/video?run_id=mock-ws-fallback",
        frame_url: "/api/leisaac/frame.jpg?run_id=mock-ws-fallback",
        input_url: "/api/leisaac/input?run_id=mock-ws-fallback",
      },
    });
    cy.intercept("POST", "/api/leisaac/select", {
      statusCode: 200,
      body: { selected: true },
    });
    cy.intercept("GET", "/api/leisaac/frame.jpg?run_id=mock-ws-fallback&frame=*", {
      statusCode: 200,
      headers: { "content-type": "image/svg+xml" },
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="#16a34a"/></svg>',
    }).as("wsFallbackFrame");
    cy.window().then((win) => {
      class FailingWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = FailingWebSocket.CONNECTING;
          win.setTimeout(() => this.onerror && this.onerror(new Error("blocked")), 0);
        }
        close() {}
      }
      win.WebSocket = FailingWebSocket;
    });
    cy.window().then((win) =>
      win.__NPA_AGENT_TEST__.refreshLeIsaacCapability("mock-ws-fallback"),
    );
    cy.wait("@wsFallbackStatus");
    cy.get("#tabLeIsaac").click();
    cy.get("#leisaacConnect").click();
    cy.wait("@wsFallbackFrame");
    cy.get("#leisaacTransportStatus", { timeout: 10000 })
      .should("contain.text", "JPEG polling")
      .and("contain.text", "fallback");
    cy.get("#leisaacFrame").should("be.visible");
    cy.get("#leisaacDisconnect").click();
  });
});
