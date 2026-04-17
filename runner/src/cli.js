/**
 * cli.js — run generated collections from the command line
 *
 * Usage:
 *   node src/cli.js --all                      run all collections/
 *   node src/cli.js --collection UsersCRUD     run a single collection
 */
const path  = require("path");
const fs    = require("fs");
const { glob }         = require("glob");
const chalk            = require("chalk");
const { runCollection } = require("./runner");

const BASE        = path.resolve(__dirname, "../..");
const COLLECTIONS = path.join(BASE, "collections");
const ENV_FILE    = path.join(BASE, "env/environment.json");

async function runAll() {
  const files = await glob(`${COLLECTIONS}/*.json`);
  if (files.length === 0) {
    console.log(chalk.yellow("No collections found in collections/. Run: python -m testgen generate"));
    process.exit(0);
  }

  let totalPassed = 0, totalFailed = 0;

  for (const file of files) {
    const name = path.basename(file, ".json");
    console.log(chalk.cyan(`\n▶  ${name}`));
    try {
      const result = await runCollection(file, ENV_FILE);
      totalPassed += result.passed;
      totalFailed += result.failed;
      const icon = result.failed === 0 ? chalk.green("✔") : chalk.red("✖");
      console.log(`${icon} ${name} — ${result.passed}/${result.total} passed | ${result.reportPath}`);
      result.failures.forEach((f) => {
        console.log(chalk.red(`   ✖ [${f.request}] ${f.test}: ${f.message}`));
      });
    } catch (err) {
      console.error(chalk.red(`✖ ${name} — ERROR: ${err.message}`));
      totalFailed++;
    }
  }

  console.log(chalk.bold(`\nSummary: ${totalPassed} passed, ${totalFailed} failed`));
  process.exit(totalFailed > 0 ? 1 : 0);
}

async function runSingle(name) {
  const filePath = path.join(COLLECTIONS, `${name}.json`);
  if (!fs.existsSync(filePath)) {
    console.error(chalk.red(`Collection not found: ${filePath}`));
    process.exit(1);
  }
  console.log(chalk.cyan(`\n▶  ${name}`));
  const result = await runCollection(filePath, ENV_FILE);
  const icon = result.failed === 0 ? chalk.green("✔") : chalk.red("✖");
  console.log(`${icon} ${result.passed}/${result.total} passed | ${result.reportPath}`);
  result.failures.forEach((f) => {
    console.log(chalk.red(`   ✖ [${f.request}] ${f.test}: ${f.message}`));
  });
  process.exit(result.failed > 0 ? 1 : 0);
}

async function main() {
  const args    = process.argv.slice(2);
  const allFlag = args.includes("--all");
  const colIdx  = args.indexOf("--collection");
  const name    = colIdx !== -1 ? args[colIdx + 1] : null;

  if (!allFlag && !name) {
    console.error("Usage:");
    console.error("  node src/cli.js --all");
    console.error("  node src/cli.js --collection <Name>");
    process.exit(1);
  }

  if (allFlag) await runAll();
  else         await runSingle(name);
}

main();
