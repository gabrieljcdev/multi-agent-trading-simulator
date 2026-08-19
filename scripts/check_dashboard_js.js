// Dev check: extract the inline <script> from ui/web_dashboard.html and
// verify it parses as JavaScript. Run: node scripts/check_dashboard_js.js
const fs = require("fs");
const html = fs.readFileSync("ui/web_dashboard.html", "utf8");
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.log("NO SCRIPT BLOCK"); process.exit(1); }
try {
  new Function(m[1]);
  console.log("JS PARSE OK");
} catch (e) {
  console.log("JS PARSE FAIL: " + e.message);
  process.exit(1);
}
