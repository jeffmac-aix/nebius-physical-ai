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
      expect(request.body).to.deep.equal({ key: "W", event: "press" });
    });
    cy.get("#leisaacInputStatus").should(
      "contain.text",
      "Keyboard events sent: 1",
    );
    cy.get("#leisaacDisconnect").click();
  });
});
