/**
 * runner.js — core Newman execution logic
 */
const newman = require("newman");
const path   = require("path");
const fs     = require("fs");

const REPORTS_DIR = path.resolve(__dirname, "../reports");
if (!fs.existsSync(REPORTS_DIR)) fs.mkdirSync(REPORTS_DIR, { recursive: true });

/**
 * @param {string} collectionPath
 * @param {string} environmentPath
 * @param {string|null} jsonExportPath  - if set, write full Newman JSON results here
 */
function runCollection(collectionPath, environmentPath, jsonExportPath = null) {
  const collectionName = path.basename(collectionPath, ".json");
  const timestamp      = new Date().toISOString().replace(/[:.]/g, "-");
  const reportPath     = path.join(REPORTS_DIR, `${collectionName}_${timestamp}.html`);

  const reporters = ["cli", "htmlextra"];
  const reporter  = { htmlextra: { export: reportPath, title: `${collectionName} — Test Report`, showOnlyFails: false } };

  if (jsonExportPath) {
    reporters.push("json");
    reporter.json = { export: jsonExportPath };
  }

  return new Promise((resolve, reject) => {
    newman.run(
      { collection: collectionPath, environment: environmentPath, reporters, reporter, insecure: true, bail: false },
      (err, summary) => {
        if (err) return reject(err);
        const stats  = summary.run.stats;

        // Enrich failures with response body for LLM debugging
        const failures = summary.run.failures.map((f) => {
          const exec      = f.at;
          const response  = exec && exec.response;
          let responseBody = null;
          if (response) {
            try { responseBody = response.stream && response.stream.toString(); } catch (_) {}
          }
          return {
            test:         f.error.test,
            message:      f.error.message,
            request:      f.source && f.source.name,
            responseBody: responseBody ? responseBody.substring(0, 500) : null,
          };
        });

        resolve({
          collectionName,
          total:    stats.assertions.total,
          passed:   stats.assertions.total - stats.assertions.failed,
          failed:   stats.assertions.failed,
          requests: stats.requests.total,
          reportPath,
          failures,
        });
      }
    );
  });
}

module.exports = { runCollection };
