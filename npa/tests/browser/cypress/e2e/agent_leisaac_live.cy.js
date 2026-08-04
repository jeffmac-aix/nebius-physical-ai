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
    cy.window({ timeout: 30000 }).then((win) =>
      win.__NPA_AGENT_TEST__.refreshLeIsaacCapability(selectedRun)
    );

    cy.get("#tabLeIsaac", { timeout: 30000 }).should("be.visible").click();
    cy.get("#panelLeIsaac")
      .should("have.class", "is-active")
      .and("have.attr", "data-run-id", selectedRun);
    cy.get("#panelLeIsaac .hint")
      .should("contain.text", "LeIsaac-SO101-PickOrange-v0")
      .and("contain.text", "RTX PRO 6000");
    cy.screenshot("01-public-leisaac-capability", { capture: "viewport" });

    cy.get("#leisaacConnect").click();
    cy.get("#leisaacStreamStatus", { timeout: 120000 }).should(
      "contain.text",
      "keyboard teleoperation active"
    );
    cy.get("#leisaacVideo", { timeout: 120000 }).should(($video) => {
      const video = $video[0];
      expect(video.readyState, "decoded video readyState").to.be.at.least(2);
      expect(video.videoWidth, "decoded video width").to.be.greaterThan(640);
      expect(video.videoHeight, "decoded video height").to.be.greaterThan(360);
    });
    cy.wait(3000);
    cy.screenshot("02-public-leisaac-live-stream", { capture: "viewport" });

    cy.get("#leisaacStreamHost")
      .click()
      .type("wwaaddqquuojl", { delay: 150 });
    cy.get("#leisaacInputStatus")
      .should("contain.text", "Keyboard events sent: 13")
      .and("contain.text", "last L");
    cy.wait(4000);
    cy.get("#leisaacVideo").should(($video) => {
      expect($video[0].readyState, "post-input decoded video").to.be.at.least(2);
    });
    cy.screenshot("03-public-leisaac-after-keyboard-input", {
      capture: "viewport",
    });
  });
});
