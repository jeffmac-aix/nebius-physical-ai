import {
  NON_STOCK_RUN_ID,
  decodePngStats,
  firstMcapPngPayload,
  mcapCameraTopicCount,
  mcapHasCompressedImage,
  mcapHasHeldoutCamera,
  mcapHasPointCloud,
} from "../support/e2e";

const MCAP_RECORDING_PATH = "/lichtblick/recordings/sim2real.mcap";

// Extensive smoke coverage for the Lichtblick MCAP viewer. These run against the
// mock agent server, which co-serves a real (small) MCAP fixture with genuine
// camera streams so "shows nothing substantive" regressions are caught at the
// data + embed layer without needing live GPU infrastructure.
describe("Lichtblick MCAP viewer (mocked smoke)", () => {
  beforeEach(() => {
    cy.visitMockAgent();
    cy.wait("@session");
    cy.wait("@simAssets");
    cy.get("#rerunBundleCover", { timeout: 20000 }).should("have.attr", "hidden");
  });

  it("co-serves a substantive MCAP with real camera streams", () => {
    cy.request({ url: MCAP_RECORDING_PATH, encoding: "binary" }).then((resp) => {
      expect(resp.status).to.eq(200);
      const body = resp.body || "";
      expect(body.length, "mcap byte length").to.be.greaterThan(10000);
      expect(mcapHasCompressedImage(body), "has foxglove.CompressedImage schema").to.be.true;
      expect(mcapHasHeldoutCamera(body), "has /heldout/camera/ topics").to.be.true;
      expect(mcapCameraTopicCount(body), "camera topic occurrences").to.be.greaterThan(3);
    });
  });

  it("includes a GPU 3D point-cloud stream for the 3D panel", () => {
    cy.request({ url: MCAP_RECORDING_PATH, encoding: "binary" }).then((resp) => {
      expect(mcapHasPointCloud(resp.body || ""), "has foxglove.PointCloud on /heldout/points").to.be
        .true;
    });
  });

  it("decodes a real (non-stub, non-noise) camera frame from the served MCAP", () => {
    cy.request({ url: MCAP_RECORDING_PATH, encoding: "binary" }).then((resp) => {
      const payload = firstMcapPngPayload(resp.body || "");
      expect(payload, "found a PNG CompressedImage payload").to.be.a("string");
      return decodePngStats(payload).then((stats) => {
        expect(stats.width, "frame width (not a 32px stub)").to.be.greaterThan(40);
        expect(stats.height, "frame height (not a 32px stub)").to.be.greaterThan(40);
        expect(stats.mean, "frame brightness (not dark noise)").to.be.greaterThan(60);
        expect(stats.mean, "frame brightness (not saturated)").to.be.lessThan(250);
      });
    });
  });

  it("feeds the embedded viewer the MCAP data source with camera topics detected", () => {
    cy.get("#tabRerun").click();
    cy.get("#renderModeLichtblick").click();
    cy.get("#viewerPaneLichtblick").should("have.class", "is-active-viewer");
    cy.get("#lichtblickFrame").should("have.attr", "src").and("include", "/lichtblick/");
    cy.get("#lichtblickFrame")
      .its("0.contentWindow.__NPA_MOCK_LICHTBLICK__.loaded", { timeout: 15000 })
      .should("eq", true);
    cy.get("#lichtblickFrame").then(($frame) => {
      const stats = $frame[0].contentWindow.__NPA_MOCK_LICHTBLICK__;
      expect(stats.hasCompressedImage, "viewer received CompressedImage frames").to.be.true;
      expect(stats.hasHeldoutCamera, "viewer received held-out camera topics").to.be.true;
      expect(stats.cameraTopicCount, "camera topics seen by viewer").to.be.greaterThan(3);
      expect(stats.mcapBytes, "mcap bytes fetched by viewer").to.be.greaterThan(10000);
    });
  });

  it("decodes the iframe ds.url to the co-served recording path", () => {
    cy.get("#tabRerun").click();
    cy.get("#renderModeLichtblick").click();
    cy.get("#lichtblickFrame")
      .invoke("attr", "src")
      .then((src) => {
        const url = new URL(String(src), "http://127.0.0.1");
        const ds = decodeURIComponent(url.searchParams.get("ds.url") || "");
        expect(ds).to.include("/lichtblick/recordings/sim2real.mcap");
      });
  });

  it("filters discovered artifacts to the MCAP (Lichtblick) type", () => {
    cy.get("#tabRerun").click();
    cy.get("#artifactPrefix").clear().type("sim2real-b/custom-assets");
    cy.get("#artifactTypeFilter").select("mcap");
    cy.get("#artifactRefreshRuns").click();
    cy.wait("@artifactRuns");
    cy.get("#runIdSelect").select(NON_STOCK_RUN_ID);
    cy.wait("@nonStockArtifactList");
    cy.get("#artifactList").should("contain.text", `${NON_STOCK_RUN_ID}/reports/sim2real.mcap`);
    cy.get("#artifactList").should("contain.text", "View in Lichtblick");
    // Non-MCAP artifacts are hidden by the type filter.
    cy.get("#artifactList").should("not.contain.text", ".rrd");
    cy.get("#artifactList").should("not.contain.text", ".mp4");
  });

  it("loads an MCAP artifact into the Lichtblick pane with an mcap summary", () => {
    cy.get("#tabRerun").click();
    cy.get("#artifactPrefix").clear().type("sim2real-b/custom-assets");
    cy.get("#artifactRefreshRuns").click();
    cy.wait("@artifactRuns");
    cy.get("#runIdSelect").select(NON_STOCK_RUN_ID);
    cy.wait("@nonStockArtifactList");
    cy.get(`#artifactList button[data-key="${NON_STOCK_RUN_ID}/reports/sim2real.mcap"]`).click();
    cy.wait("@loadArtifact");
    cy.get("#renderModeLichtblick").should("have.class", "is-active");
    cy.get("#viewerPaneLichtblick").should("have.class", "is-active-viewer");
    cy.get("#renderedDataSummary").should("contain.text", "mcap");
    cy.get("#lichtblickFrame").should("have.attr", "src").and("include", "/lichtblick/");
  });

  it("keeps both viewers mounted when switching render modes", () => {
    cy.get("#tabRerun").click();
    cy.get("#renderModeLichtblick").click();
    cy.get("#viewerPaneLichtblick").should("have.class", "is-active-viewer");
    cy.get("#lichtblickFrame")
      .its("0.contentWindow.__NPA_MOCK_LICHTBLICK__", { timeout: 15000 })
      .should("exist");
    cy.get("#lichtblickFrame").then(($frame) => {
      const canvas = $frame[0].contentDocument.querySelector('[data-testid="mock-lichtblick-canvas"]');
      expect(canvas.width, "canvas width").to.be.greaterThan(0);
      expect(canvas.height, "canvas height").to.be.greaterThan(0);
    });
    cy.get("#renderModeRerun").click();
    cy.get("#viewerPaneLichtblick").should("have.class", "is-inactive-viewer");
    cy.get("#lichtblickFrame").should("exist");
    cy.get("#renderModeLichtblick").click();
    cy.get("#viewerPaneLichtblick").should("have.class", "is-active-viewer");
  });

  it("re-mounts the Lichtblick iframe on Reload without leaving the pane", () => {
    cy.get("#tabRerun").click();
    cy.get("#renderModeLichtblick").click();
    cy.get("#loadLichtblickViewer").click();
    cy.get("#viewerPaneLichtblick").should("have.class", "is-active-viewer");
    cy.get("#lichtblickFrame").should("have.attr", "src").and("include", "/lichtblick/");
    cy.get("#lichtblickFrame")
      .its("0.contentWindow.__NPA_MOCK_LICHTBLICK__.loaded", { timeout: 15000 })
      .should("eq", true);
  });

  it("opens the Lichtblick viewer in a new tab from Open in Lichtblick", () => {
    cy.get("#tabRerun").click();
    cy.get("#renderModeLichtblick").click();
    cy.window().then((win) => {
      cy.stub(win, "open").as("windowOpen");
    });
    cy.get("#openLichtblick").click();
    cy.get("@windowOpen").should("have.been.called");
  });
});
