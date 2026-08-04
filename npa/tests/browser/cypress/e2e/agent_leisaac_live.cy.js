const requiredLiveEnv = [
  "NPA_AGENT_BASE_URL",
  "NPA_AGENT_USER",
  "NPA_AGENT_PASSWORD",
  "NPA_AGENT_RUN_ID",
];

function hasLiveEnv() {
  return requiredLiveEnv.every((name) => Boolean(Cypress.env(name)));
}

function runId() {
  return String(Cypress.env("NPA_AGENT_RUN_ID") || "").trim();
}

(hasLiveEnv() ? describe : describe.skip)("NPA agent live LeIsaac teleoperation", () => {
  beforeEach(() => {
    cy.viewport(1440, 1050);
    cy.visitLiveAgent();
  });

  it("discovers, renders, and controls PickOrange through public authenticated HTTPS", () => {
    const selectedRun = runId();
    // Exercise the supported run picker so periodic capability refreshes stay
    // pinned to this live run instead of the agent's previously active run.
    cy.get("#stagesRunInput", { timeout: 30000 })
      .clear()
      .type(selectedRun, { delay: 0 });
    cy.get("#stagesLoadRun").click();

    cy.get("#tabLeIsaac", { timeout: 30000 }).should("be.visible").click();
    cy.get("#panelLeIsaac")
      .should("have.class", "is-active")
      .and("have.attr", "data-run-id", selectedRun);
    cy.get("#panelLeIsaac .hint")
      .should("contain.text", "LeIsaac-SO101-PickOrange-v0")
      .and("contain.text", "RTX PRO 6000");
    cy.screenshot("01-public-leisaac-capability", { capture: "viewport" });

    // Prove the browser can authenticate to the session-scoped TURN service
    // and gather a relay candidate before the NVIDIA client consumes it.
    cy.window().then(async (win) => {
      const response = await win.fetch(
        "/api/leisaac/status?run_id=" + encodeURIComponent(selectedRun),
        { credentials: "include" }
      );
      const status = await response.json();
      const peer = new win.RTCPeerConnection({
        iceServers: status.ice_servers,
        iceTransportPolicy: "relay",
      });
      peer.createDataChannel("npa-turn-preflight");
      const result = await new Promise(async (resolve, reject) => {
        const timer = win.setTimeout(
          () => reject(new Error("TURN preflight did not gather a relay candidate")),
          20000
        );
        peer.addEventListener("icecandidate", (event) => {
          const candidate = String((event.candidate && event.candidate.candidate) || "");
          if (candidate.includes(" typ relay ")) {
            win.clearTimeout(timer);
            resolve({ relay: true });
          }
        });
        peer.addEventListener("icecandidateerror", (event) => {
          win.__LEISAAC_TURN_ERROR__ = {
            code: Number(event.errorCode || 0),
            text: String(event.errorText || ""),
            url: String(event.url || "").replace(/[^:]+:[^@]+@/, "REDACTED@"),
          };
        });
        try {
          await peer.setLocalDescription(await peer.createOffer());
        } catch (error) {
          win.clearTimeout(timer);
          reject(error);
        }
      }).finally(() => peer.close());
      win.__LEISAAC_TURN_PREFLIGHT__ = result;
    });
    cy.window().its("__LEISAAC_TURN_PREFLIGHT__.relay").should("eq", true);

    // Cypress injects cy.visit({ auth }) at its network proxy, which does not
    // populate Chromium's HTTP-auth cache for JavaScript-created WebSockets.
    // A normal browser already has that cache after the Basic-auth prompt. For
    // live automation, put the same credentials in only the in-memory WSS URL;
    // Chromium converts them to Authorization before nginx and never exposes
    // them through the application API or committed evidence.
    cy.window().then((win) => {
      const NativeWebSocket = win.WebSocket;
      const user = String(Cypress.env("NPA_AGENT_USER") || "");
      const password = String(Cypress.env("NPA_AGENT_PASSWORD") || "");
      class AuthenticatedWebSocket extends NativeWebSocket {
        constructor(url, protocols) {
          const target = new win.URL(String(url));
          target.username = user;
          target.password = password;
          super(target.toString(), protocols);
        }
      }
      win.WebSocket = AuthenticatedWebSocket;
      const NativePeerConnection = win.RTCPeerConnection;
      win.__LEISAAC_LIVE_PEERS__ = [];
      function InspectablePeerConnection(configuration, constraints) {
        const config = configuration || {};
        win.__LEISAAC_LIVE_PEER_CONFIG__ = {
          iceTransportPolicy: String(config.iceTransportPolicy || ""),
          iceServers: (config.iceServers || []).map((server) => ({
            urls: Array.isArray(server.urls) ? server.urls.map(String) : [String(server.urls || "")],
            username: String(server.username || ""),
            hasCredential: Boolean(server.credential),
          })),
        };
        const peer = new NativePeerConnection(configuration, constraints);
        const diagnostic = {
          connectionState: peer.connectionState,
          iceConnectionState: peer.iceConnectionState,
          iceGatheringState: peer.iceGatheringState,
          signalingState: peer.signalingState,
          candidates: [],
          errors: [],
          tracks: [],
          inbound: [],
          candidatePairs: [],
        };
        win.__LEISAAC_LIVE_PEERS__.push(diagnostic);
        const update = () => {
          diagnostic.connectionState = peer.connectionState;
          diagnostic.iceConnectionState = peer.iceConnectionState;
          diagnostic.iceGatheringState = peer.iceGatheringState;
          diagnostic.signalingState = peer.signalingState;
        };
        [
          "connectionstatechange",
          "iceconnectionstatechange",
          "icegatheringstatechange",
          "signalingstatechange",
        ].forEach((eventName) => peer.addEventListener(eventName, update));
        peer.addEventListener("icecandidate", (event) => {
          const candidate = String((event.candidate && event.candidate.candidate) || "");
          if (candidate) diagnostic.candidates.push(candidate.replace(/\s+/g, " "));
          update();
        });
        peer.addEventListener("icecandidateerror", (event) => {
          diagnostic.errors.push({
            code: Number(event.errorCode || 0),
            text: String(event.errorText || ""),
            url: String(event.url || "").replace(/[^:]+:[^@]+@/, "REDACTED@"),
          });
          update();
        });
        peer.addEventListener("track", (event) => {
          diagnostic.tracks.push({
            kind: String((event.track && event.track.kind) || "unknown"),
            muted: Boolean(event.track && event.track.muted),
            readyState: String((event.track && event.track.readyState) || ""),
            streams: Number((event.streams && event.streams.length) || 0),
          });
          update();
        });
        const statsTimer = win.setInterval(async () => {
          if (peer.connectionState === "closed") {
            win.clearInterval(statsTimer);
            return;
          }
          try {
            const stats = await peer.getStats();
            diagnostic.inbound = [];
            diagnostic.candidatePairs = [];
            stats.forEach((report) => {
              if (report.type === "inbound-rtp") {
                diagnostic.inbound.push({
                  kind: String(report.kind || report.mediaType || ""),
                  bytesReceived: Number(report.bytesReceived || 0),
                  packetsReceived: Number(report.packetsReceived || 0),
                  framesDecoded: Number(report.framesDecoded || 0),
                });
              }
              if (report.type === "candidate-pair" && (report.nominated || report.state === "succeeded")) {
                diagnostic.candidatePairs.push({
                  state: String(report.state || ""),
                  nominated: Boolean(report.nominated),
                  bytesSent: Number(report.bytesSent || 0),
                  bytesReceived: Number(report.bytesReceived || 0),
                  localCandidateId: String(report.localCandidateId || ""),
                  remoteCandidateId: String(report.remoteCandidateId || ""),
                });
              }
            });
          } catch (_error) {
            // A peer may close while the diagnostic snapshot is in flight.
          }
        }, 2000);
        return peer;
      }
      InspectablePeerConnection.prototype = NativePeerConnection.prototype;
      Object.setPrototypeOf(InspectablePeerConnection, NativePeerConnection);
      win.RTCPeerConnection = InspectablePeerConnection;
    });
    cy.get("#leisaacConnect").click();
    cy.window()
      .its("__LEISAAC_LIVE_PEER_CONFIG__", { timeout: 60000 })
      .should((config) => {
        expect(config.iceTransportPolicy).to.equal("relay");
        expect(config.iceServers).to.have.length(1);
        expect(config.iceServers[0].urls[0]).to.match(/^turn:[0-9.]+:3478\?transport=udp$/);
        expect(config.iceServers[0].username).to.equal(selectedRun);
        expect(config.iceServers[0].hasCredential).to.equal(true);
      });
    cy.get("#leisaacStreamStatus", { timeout: 120000 }).should(
      "contain.text",
      "keyboard teleoperation active"
    );
    cy.window().then((win) => {
      cy.get("#leisaacVideo", { timeout: 120000 }).should(($video) => {
        const video = $video[0];
        const peerDiagnostic = JSON.stringify(win.__LEISAAC_LIVE_PEERS__ || []);
        expect(video.readyState, `decoded video readyState; peers=${peerDiagnostic}`).to.be.at.least(2);
        expect(video.videoWidth, "decoded video width").to.be.greaterThan(640);
        expect(video.videoHeight, "decoded video height").to.be.greaterThan(360);
      });
    });
    cy.wait(3000);
    cy.window().then((win) => {
      cy.get("#leisaacVideo", { timeout: 60000 }).should(($video) => {
        const video = $video[0];
        const canvas = win.document.createElement("canvas");
        canvas.width = 160;
        canvas.height = 90;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
        let minimum = 255;
        let maximum = 0;
        let total = 0;
        for (let index = 0; index < pixels.length; index += 4) {
          const luma = Math.round(
            (pixels[index] * 0.2126) +
            (pixels[index + 1] * 0.7152) +
            (pixels[index + 2] * 0.0722)
          );
          minimum = Math.min(minimum, luma);
          maximum = Math.max(maximum, luma);
          total += luma;
        }
        const frame = {
          minimum,
          maximum,
          mean: Math.round(total / (pixels.length / 4)),
        };
        const peerDiagnostic = JSON.stringify(win.__LEISAAC_LIVE_PEERS__ || []);
        expect(maximum - minimum, `rendered frame variance; frame=${JSON.stringify(frame)}; peers=${peerDiagnostic}`).to.be.greaterThan(12);
        expect(frame.mean, "rendered frame mean luma").to.be.greaterThan(3);
      });
    });
    cy.screenshot("02-public-leisaac-live-stream", { capture: "viewport" });

    cy.get("#leisaacStreamHost")
      .click()
      .type("wwaaddqquuojl", { delay: 150 });
    cy.get("#leisaacInputStatus")
      .should("contain.text", "Keyboard events sent: 13")
      .and("contain.text", "last L");
    cy.window().then(async (win) => {
      const deadline = Date.now() + 30000;
      while (Date.now() < deadline) {
        const response = await win.fetch(
          "/api/leisaac/status?run_id=" + encodeURIComponent(selectedRun),
          { credentials: "include", cache: "no-store" }
        );
        const status = await response.json();
        if (Number(status.input_events || 0) >= 13) {
          win.__LEISAAC_SERVER_INPUT_EVENTS__ = Number(status.input_events);
          return;
        }
        await new Promise((resolve) => win.setTimeout(resolve, 1000));
      }
      throw new Error("LeIsaac server did not attest the keyboard input events");
    });
    cy.window().its("__LEISAAC_SERVER_INPUT_EVENTS__").should("be.at.least", 13);
    cy.wait(4000);
    cy.get("#leisaacVideo").should(($video) => {
      expect($video[0].readyState, "post-input decoded video").to.be.at.least(2);
    });
    cy.screenshot("03-public-leisaac-after-keyboard-input", {
      capture: "viewport",
    });
  });
});
