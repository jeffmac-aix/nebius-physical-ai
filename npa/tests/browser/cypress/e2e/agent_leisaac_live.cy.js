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

    cy.window().then(async (win) => {
      const response = await win.fetch(
        "/api/leisaac/status?run_id=" + encodeURIComponent(selectedRun),
        { credentials: "include", cache: "no-store" }
      );
      expect(response.ok, "authenticated capability status").to.equal(true);
      const status = await response.json();
      expect(status.available).to.equal(true);
      expect(status.stream_transport).to.equal("jpeg-poll");
      expect(status.frame_url).to.match(/^\/api\/leisaac\/frame\.jpg\?run_id=/);
      expect(status.input_url).to.match(/^\/api\/leisaac\/input\?run_id=/);
      expect(status.gpu).to.contain("RTX PRO 6000");
      expect(status.frame_bytes).to.be.greaterThan(10000);
      win.__LEISAAC_INITIAL_STATUS__ = status;
    });

    cy.get("#leisaacConnect").click();
    cy.get("#leisaacStreamStatus", { timeout: 120000 }).should(
      "contain.text",
      "keyboard teleoperation active"
    );
    cy.get("#leisaacFrame", { timeout: 120000 })
      .should("be.visible")
      .and(($frame) => {
        expect($frame[0].complete, "decoded RTX frame").to.equal(true);
        expect($frame[0].naturalWidth, "decoded frame width").to.be.greaterThan(640);
        expect($frame[0].naturalHeight, "decoded frame height").to.be.greaterThan(360);
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

      const frame = win.document.getElementById("leisaacFrame");
      const canvas = win.document.createElement("canvas");
      canvas.width = 160;
      canvas.height = 90;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      context.drawImage(frame, 0, 0, canvas.width, canvas.height);
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
      expect(maximum - minimum, "rendered frame variance").to.be.greaterThan(12);
      expect(Math.round(total / (pixels.length / 4)), "rendered frame mean luma").to.be.greaterThan(3);
    });
    cy.screenshot("02-public-leisaac-live-stream", { capture: "viewport" });

    cy.get("#leisaacStreamHost")
      .click()
      .type("wwaaddqquuojl", { delay: 150 });
    cy.get("#leisaacInputStatus")
      .should("contain.text", "Keyboard events sent: 13")
      .and("contain.text", "last L");
    cy.window().then(async (win) => {
      const initial = win.__LEISAAC_INITIAL_STATUS__;
      const deadline = Date.now() + 30000;
      while (Date.now() < deadline) {
        const response = await win.fetch(
          "/api/leisaac/status?run_id=" + encodeURIComponent(selectedRun),
          { credentials: "include", cache: "no-store" }
        );
        const status = await response.json();
        if (
          Number(status.input_events || 0) >= Number(initial.input_events || 0) + 13 &&
          Number(status.applied_inputs || 0) >= Number(initial.applied_inputs || 0) + 13 &&
          String(status.frame_updated_at || "") > String(initial.frame_updated_at || "")
        ) {
          win.__LEISAAC_SERVER_INPUT_EVENTS__ = Number(status.input_events);
          win.__LEISAAC_APPLIED_INPUTS__ = Number(status.applied_inputs);
          return;
        }
        await new Promise((resolve) => win.setTimeout(resolve, 500));
      }
      throw new Error("LeIsaac did not apply the browser keyboard inputs to the live simulator");
    });
    cy.window().its("__LEISAAC_SERVER_INPUT_EVENTS__").should("be.at.least", 13);
    cy.window().its("__LEISAAC_APPLIED_INPUTS__").should("be.at.least", 13);
    cy.get("#leisaacFrame").should(($frame) => {
      expect($frame[0].complete, "post-input decoded frame").to.equal(true);
      expect($frame[0].naturalWidth).to.be.greaterThan(640);
    });
    cy.screenshot("03-public-leisaac-after-keyboard-input", {
      capture: "viewport",
    });
  });
});
