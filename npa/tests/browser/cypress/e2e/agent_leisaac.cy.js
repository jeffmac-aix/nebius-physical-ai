describe("NPA agent LeIsaac capability tab", () => {
  beforeEach(() => {
    cy.visitMockAgent();
    cy.wait("@session");
  });

  it("is absent when the selected run has no usable live capability", () => {
    cy.get("#tabLeIsaac").should("not.exist");
    cy.get("#panelLeIsaac").should("not.exist");
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
        media_server: "1.1.1.1",
        media_port: 47998,
        ice_transport_policy: "relay",
        ice_servers: [{
          urls: ["turn:1.1.1.1:3478?transport=udp"],
          username: "mock-run",
          credential: "ephemeral-test-credential",
        }],
        signaling_server: "same-origin",
        signaling_port: 443,
        signaling_path: "/api/leisaac/signal",
        client_module_url: "/api/leisaac/client/index.js?run_id=mock-run",
        source_version: "0.4.0",
        source_commit: "1651c321e9b0c1bb54233211fc7b3cd70d8373d5",
        isaac_sim_version: "5.1.0.0",
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

    cy.window().then((win) => win.__NPA_AGENT_TEST__.refreshLeIsaacCapability("mock-run"));
    cy.wait("@leisaacStatus");
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
    cy.get("#leisaacStreamStatus").should("contain.text", "keyboard teleoperation active");
    cy.window().its("__LEISAAC_CONNECT_PROPS__.streamConfig.signalingPath").should("eq", "/api/leisaac/signal");
    cy.window().its("__LEISAAC_CONNECT_PROPS__.streamConfig.forceWSS").should("eq", true);
    cy.window().its("__LEISAAC_CONNECT_PROPS__.streamConfig.mediaPort").should("eq", 47998);
    cy.window().its("__LEISAAC_PEER_CONFIG__.iceTransportPolicy").should("eq", "relay");
    cy.window().its("__LEISAAC_PEER_CONFIG__.iceServers.0.urls.0").should("eq", "turn:1.1.1.1:3478?transport=udp");
    cy.get("#leisaacStreamHost").trigger("keydown", { key: "W", code: "KeyW" });
    cy.get("#leisaacInputStatus").should("contain.text", "Keyboard events sent: 1").and("contain.text", "last W");

    cy.intercept("GET", "/api/leisaac/status?run_id=mock-run", {
      statusCode: 200,
      body: { available: false, reason: "session expired" },
    }).as("leisaacGone");
    cy.window().then((win) => win.__NPA_AGENT_TEST__.refreshLeIsaacCapability("mock-run"));
    cy.wait("@leisaacGone");
    cy.get("#tabLeIsaac").should("not.exist");
    cy.get("#panelLeIsaac").should("not.exist");
    cy.window().then((win) => expect(win.RTCPeerConnection).to.equal(win.__LEISAAC_NATIVE_PEER__));
  });
});
