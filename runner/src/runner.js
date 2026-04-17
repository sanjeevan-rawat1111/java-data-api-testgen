/**
 * runner.js — core Newman execution logic
 */
const newman = require("newman");
const path   = require("path");
const fs     = require("fs");

const REPORTS_DIR = path.resolve(__dirname, "../reports");
if (!fs.existsSync(REPORTS_DIR)) fs.mkdirSync(REPORTS_DIR, { recursive: true });

function runCollection(collectionPath, environmentPath) {
  const collectionName = path.basename(collectionPath, ".json");
  const timestamp      = new Date().toISOString().replace(/[:.]/g, "-");
  const reportPath     = path.join(REPORTS_DIR, `${collectionName}_${timestamp}.html`);

  return new Promise((resolve, reject) => {
    newman.run(
      {
        collection:   collectionPath,
        environment:  environmentPath,
        reporters:    ["cli", "htmlextra"],
        reporter:     { htmlextra: { export: reportPath, title: `${collectionName} — Test Report`, showOnlyFails: false } },
        insecure:     true,
        bail:         false,
      },
      (err, summary) => {
        if (err) return reject(err);
        const stats  = summary.run.stats;
        const result = {
          collectionName,
          total:    stats.assertions.total,
          passed:   stats.assertions.total - stats.assertions.failed,
          failed:   stats.assertions.failed,
          requests: stats.requests.total,
          reportPath,
          failures: summary.run.failures.map((f) => ({
            test:    f.error.test,
            message: f.error.message,
            request: f.source && f.source.name,
          })),
        };
        resolve(result);
      }
    );
  });
}

module.exports = { runCollection };
