const requiredLiveEnv = [
  "NPA_AGENT_BASE_URL",
  "NPA_AGENT_USER",
  "NPA_AGENT_PASSWORD",
  "NPA_AGENT_RUN_ID",
  "NPA_AGENT_TASK",
];

function hasLiveEnv() {
  return requiredLiveEnv.every((name) => Boolean(Cypress.env(name)));
}

function runId() {
  return String(Cypress.env("NPA_AGENT_RUN_ID") || "").trim();
}

function expectedTask() {
  return String(
    Cypress.env("NPA_AGENT_TASK") || "LeIsaac-SO101-PickOrange-v0",
  ).trim();
}

function expectedEnvironment() {
  return String(Cypress.env("NPA_AGENT_ENVIRONMENT_ID") || "").trim();
}

function expectedCompletedEpisodes() {
  return Number(Cypress.env("NPA_AGENT_COMPLETED_EPISODES") || 0);
}

function loadRecorderStatus() {
  const selectedRun = runId();
  return cy.window().then(async (win) => {
    const response = await win.fetch(
      "/api/leisaac/status?run_id=" + encodeURIComponent(selectedRun),
      { credentials: "include", cache: "no-store" },
    );
    if (!response.ok) {
      throw new Error(
        "recorder status returned HTTP " + String(response.status),
      );
    }
    return response.json();
  });
}

function waitForRecorder(predicate) {
  return loadRecorderStatus().then((status) => {
    const recorder = status.recorder || {};
    if (predicate(recorder, status)) return status;
    if (recorder.last_error) {
      throw new Error(
        "recorder transition failed: " + String(recorder.last_error),
      );
    }
    return cy.wait(500).then(() => waitForRecorder(predicate));
  });
}

function dispatchTeleoperation(keys) {
  cy.get("#leisaacStreamHost")
    .click()
    .then(($host) => {
      for (const key of keys) {
        $host[0].dispatchEvent(
          new KeyboardEvent("keydown", { key, bubbles: true }),
        );
        $host[0].dispatchEvent(
          new KeyboardEvent("keyup", { key, bubbles: true }),
        );
      }
    });
}

function recordEpisode(outcome, episodeNumber, completedBefore) {
  cy.get("#leisaacRecordStart").should("not.be.disabled").click();
  return waitForRecorder(
    (recorder) =>
      recorder.state === "recording" &&
      Boolean(recorder.active_episode) &&
      Number(recorder.frame_count || 0) >= 2,
  )
    .then((recordingStatus) => {
      cy.get("#leisaacRecorderStatus")
        .should("contain.text", "State: recording")
        .and("not.contain.text", "active: none");
      cy.get("#leisaacRecordStart").should("be.disabled");
      cy.get("#leisaacRecordSuccess").should("not.be.disabled");
      cy.get("#leisaacRecordFailure").should("not.be.disabled");
      cy.get("#leisaacRecordFinalize")
        .should("be.disabled")
        .and("have.attr", "title")
        .and("contain", "Mark success or failure");
      cy.get("#leisaacRecorderGuidance").should(
        "contain.text",
        "Recording live simulator frames",
      );
      cy.screenshot(
        `0${4 + episodeNumber * 4}-episode-${episodeNumber}-${outcome}-recording`,
        { capture: "viewport" },
      );

      const inputBefore = Number(recordingStatus.input_events || 0);
      const framesBefore = Number(recordingStatus.recorder.frame_count || 0);
      dispatchTeleoperation(
        outcome === "success" ? ["W", "A", "U"] : ["S", "D", "O"],
      );
      return waitForRecorder(
        (recorder, status) =>
          recorder.state === "recording" &&
          Number(recorder.frame_count || 0) > framesBefore &&
          Number(status.input_events || 0) >= inputBefore + 3 &&
          Number(status.input_events || 0) ===
            Number(status.applied_inputs || 0),
      );
    })
    .then((appliedStatus) => {
      expect(appliedStatus.input_events, "accepted inputs").to.equal(
        appliedStatus.applied_inputs,
      );
      cy.get(
        outcome === "success"
          ? "#leisaacRecordSuccess"
          : "#leisaacRecordFailure",
      )
        .should("not.be.disabled")
        .click();
      return waitForRecorder(
        (recorder) =>
          recorder.state === "outcome-pending" &&
          recorder.pending_outcome === outcome,
      );
    })
    .then(() => {
      cy.get("#leisaacRecorderStatus").should(
        "contain.text",
        "State: outcome-pending",
      );
      cy.get("#leisaacRecorderGuidance").should(
        "contain.text",
        `Outcome selected: ${outcome}`,
      );
      cy.get("#leisaacRecordFinalize").should("not.be.disabled");
      cy.get("#leisaacRecordStart").should("be.disabled");
      cy.screenshot(
        `0${5 + episodeNumber * 4}-episode-${episodeNumber}-${outcome}-selected`,
        { capture: "viewport" },
      );
      cy.get("#leisaacRecordFinalize").click();
      return waitForRecorder(
        (recorder) =>
          recorder.state === "idle" &&
          recorder.last_upload_status === "uploaded" &&
          recorder.last_outcome === outcome &&
          Number(recorder.completed_episode_count || 0) ===
            completedBefore + 1 &&
          Boolean(recorder.dataset_version_uri) &&
          Boolean(recorder.last_episode_commit_uri),
      );
    })
    .then((completedStatus) => {
      cy.get("#leisaacRecorderStatus")
        .should("contain.text", "State: idle")
        .and("contain.text", `completed: ${completedBefore + 1}`)
        .and("contain.text", `${outcome}/uploaded`);
      cy.get("#leisaacRecorderArtifact")
        .should("contain.text", "Immutable dataset")
        .and(
          "contain.text",
          String(completedStatus.recorder.dataset_version_uri),
        );
      cy.get("#leisaacRecorderGuidance").should(
        "contain.text",
        "Upload complete",
      );
      cy.get("#leisaacRecordStart").should("not.be.disabled");
      cy.get("#leisaacRecordFinalize").should("be.disabled");
      cy.screenshot(
        `0${6 + episodeNumber * 4}-episode-${episodeNumber}-${outcome}-uploaded`,
        { capture: "viewport" },
      );
    });
}

(hasLiveEnv() ? describe : describe.skip)(
  "NPA agent live LeIsaac teleoperation",
  () => {
    beforeEach(() => {
      cy.viewport(1440, 1050);
      cy.visitLiveAgent();
    });

    it("discovers, renders, and controls the selected task through public authenticated HTTPS", () => {
      const selectedRun = runId();
      const selectedTask = expectedTask();
      const selectedEnvironment = expectedEnvironment();
      const completedEpisodes = expectedCompletedEpisodes();
      cy.get("#tabLeIsaac", { timeout: 30000 }).should("be.visible").click();
      cy.get("#panelLeIsaac")
        .should("have.class", "is-active")
        .and("have.attr", "data-run-id", selectedRun);
      cy.get("#panelLeIsaac .hint")
        .should("contain.text", selectedTask)
        .and("contain.text", selectedEnvironment)
        .and("contain.text", "RTX PRO 6000");
      cy.get("#leisaacRecorderStatus").should("contain.text", "State: idle");
      if (completedEpisodes > 0) {
        cy.get("#leisaacRecorderStatus")
          .should("contain.text", `completed: ${completedEpisodes}`)
          .and("contain.text", "uploaded");
      }
      cy.screenshot("01-public-leisaac-capability", { capture: "viewport" });

      cy.window().then(async (win) => {
        const response = await win.fetch(
          "/api/leisaac/status?run_id=" + encodeURIComponent(selectedRun),
          { credentials: "include", cache: "no-store" },
        );
        expect(response.ok, "authenticated capability status").to.equal(true);
        const status = await response.json();
        expect(status.available).to.equal(true);
        expect(status.task).to.equal(selectedTask);
        expect(status.environment_id).to.equal(selectedEnvironment);
        expect(status.recorder.state).to.equal("idle");
        if (completedEpisodes > 0) {
          expect(status.recorder.completed_episode_count).to.equal(
            completedEpisodes,
          );
        }
        expect(status.stream_transport).to.equal("websocket-v1");
        expect(status.control_ws_url).to.match(
          /^\/api\/leisaac\/transport\/control\?run_id=/,
        );
        expect(status.video_ws_url).to.match(
          /^\/api\/leisaac\/transport\/video\?run_id=/,
        );
        expect(status.frame_url).to.match(
          /^\/api\/leisaac\/frame\.jpg\?run_id=/,
        );
        expect(status.input_url).to.match(/^\/api\/leisaac\/input\?run_id=/);
        expect(status.gpu).to.contain("RTX PRO 6000");
        expect(status.frame_bytes).to.be.greaterThan(10000);
        win.__LEISAAC_INITIAL_STATUS__ = status;
      });

      cy.get("#leisaacConnect").click();
      cy.get("#leisaacStreamStatus", { timeout: 120000 }).should(
        "contain.text",
        "keyboard teleoperation active",
      );
      cy.get("#leisaacTransportStatus", { timeout: 120000 })
        .should("contain.text", "WebSocket")
        .and("contain.text", "preferred")
        .and("contain.text", "latest-frame-wins");
      cy.get("#leisaacLatencyStatus")
        .should("contain.text", "Latency: control")
        .and("contain.text", "FPS")
        .and("contain.text", "dropped/coalesced");
      cy.get("#leisaacCanvas", { timeout: 120000 })
        .should("be.visible")
        .and(($canvas) => {
          expect($canvas[0].width, "decoded frame width").to.be.greaterThan(640);
          expect($canvas[0].height, "decoded frame height").to.be.greaterThan(360);
        });

      cy.window().then(async (win) => {
        const status = win.__LEISAAC_INITIAL_STATUS__;
        const response = await win.fetch(status.frame_url + "&proof=1", {
          credentials: "include",
          cache: "no-store",
        });
        expect(response.ok, "authenticated frame route").to.equal(true);
        expect(response.headers.get("content-type")).to.contain("image/jpeg");
        const bytes = new Uint8Array(await response.arrayBuffer());
        expect(bytes.length, "real frame bytes").to.be.greaterThan(10000);
        expect(bytes[0]).to.equal(0xff);
        expect(bytes[1]).to.equal(0xd8);

        const bitmap = await win.createImageBitmap(
          new win.Blob([bytes], { type: "image/jpeg" }),
        );
        const canvas = win.document.createElement("canvas");
        canvas.width = 160;
        canvas.height = 90;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
        bitmap.close();
        const pixels = context.getImageData(
          0,
          0,
          canvas.width,
          canvas.height,
        ).data;
        let minimum = 255;
        let maximum = 0;
        let total = 0;
        for (let index = 0; index < pixels.length; index += 4) {
          const luma = Math.round(
            pixels[index] * 0.2126 +
              pixels[index + 1] * 0.7152 +
              pixels[index + 2] * 0.0722,
          );
          minimum = Math.min(minimum, luma);
          maximum = Math.max(maximum, luma);
          total += luma;
        }
        expect(maximum - minimum, "rendered frame variance").to.be.greaterThan(
          12,
        );
        expect(
          Math.round(total / (pixels.length / 4)),
          "rendered frame mean luma",
        ).to.be.greaterThan(3);
      });
      cy.screenshot("02-public-leisaac-live-stream", { capture: "viewport" });

      const controls = [
        "W",
        "S",
        "A",
        "D",
        "Q",
        "E",
        "U",
        "O",
        "J",
        "L",
        "I",
        "K",
        "L",
      ];
      cy.get("#leisaacStreamHost")
        .click()
        .then(($host) => {
          for (const key of controls) {
            $host[0].dispatchEvent(
              new KeyboardEvent("keydown", { key, bubbles: true }),
            );
            $host[0].dispatchEvent(
              new KeyboardEvent("keyup", { key, bubbles: true }),
            );
          }
        });
      cy.get("#leisaacInputStatus")
        .should("contain.text", "Keyboard events sent: 13")
        .and("contain.text", "last L");
      cy.window().then(async (win) => {
        const initial = win.__LEISAAC_INITIAL_STATUS__;
        while (true) {
          const response = await win.fetch(
            "/api/leisaac/status?run_id=" + encodeURIComponent(selectedRun),
            { credentials: "include", cache: "no-store" },
          );
          const status = await response.json();
          if (
            Number(status.input_events || 0) >=
              Number(initial.input_events || 0) + 13 &&
            Number(status.applied_inputs || 0) >=
              Number(initial.applied_inputs || 0) + 13 &&
            Number(status.input_events || 0) ===
              Number(status.applied_inputs || 0) &&
            String(status.frame_updated_at || "") >
              String(initial.frame_updated_at || "")
          ) {
            win.__LEISAAC_SERVER_INPUT_EVENTS__ = Number(status.input_events);
            win.__LEISAAC_APPLIED_INPUTS__ = Number(status.applied_inputs);
            return;
          }
          await new Promise((resolve) => win.setTimeout(resolve, 500));
        }
      });
      cy.window()
        .its("__LEISAAC_SERVER_INPUT_EVENTS__")
        .should("be.at.least", 13);
      cy.window().its("__LEISAAC_APPLIED_INPUTS__").should("be.at.least", 13);
      cy.window().then((win) => {
        expect(win.__LEISAAC_SERVER_INPUT_EVENTS__, "accepted inputs").to.equal(
          win.__LEISAAC_APPLIED_INPUTS__,
        );
      });
      cy.get("#leisaacCanvas").should(($canvas) => {
        expect($canvas[0].width, "post-input decoded frame").to.be.greaterThan(640);
        expect($canvas[0].height).to.be.greaterThan(360);
      });
      cy.screenshot("03-public-leisaac-after-keyboard-input", {
        capture: "viewport",
      });

      cy.get("#leisaacRecordStart").should("not.be.disabled");
      cy.get("#leisaacRecordSuccess")
        .should("be.disabled")
        .and("have.attr", "title")
        .and("contain", "Start an episode");
      cy.get("#leisaacRecordFinalize")
        .should("be.disabled")
        .and("have.attr", "title")
        .and("contain", "Mark success or failure");
      cy.get("#leisaacRecorderGuidance")
        .invoke("text")
        .should("match", /Start (?:an )?episode/i);
      cy.screenshot("04-recorder-idle-start-enabled", { capture: "viewport" });

      recordEpisode("success", 0, completedEpisodes).then(() =>
        recordEpisode("failure", 1, completedEpisodes + 1),
      );
    });
  },
);
