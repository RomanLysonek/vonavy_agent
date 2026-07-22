import fs from "node:fs";
import vm from "node:vm";

class Node {
  constructor(tag = "#text", text = "") {
    this.tagName = tag.toUpperCase();
    this.textContent = text;
    this.children = [];
    this.className = "";
    this.dataset = {};
  }
  append(...values) { this.children.push(...values); }
  replaceChildren(...values) { this.children = [...values]; }
}

const document = {
  createElement: (tag) => new Node(tag),
  createTextNode: (text) => new Node("#text", text),
};
const source = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const start = source.indexOf("function appendInlineMarkdown");
const end = source.indexOf("function renderAgentPlan", start);
if (start < 0 || end < 0) throw new Error("Markdown renderer anchors are missing");
const context = vm.createContext({ document, URL, location: { origin: "https://example.invalid" } });
vm.runInContext(source.slice(start, end), context);
const root = new Node("article");
context.renderSafeMarkdown(
  root,
  "## Columns\n\n| Column | Type |\n|---|---|\n| **ProductId** | `numeric` |\n\n<script>alert(1)</script>",
);
const tags = [];
const texts = [];
function walk(node) {
  tags.push(node.tagName);
  texts.push(node.textContent);
  for (const child of node.children) walk(child);
}
walk(root);
if (!tags.includes("H2") || !tags.includes("TABLE") || !tags.includes("STRONG")) {
  throw new Error(`missing rendered tags: ${tags.join(",")}`);
}
if (tags.includes("SCRIPT")) throw new Error("unsafe script element was created");
if (!texts.some((value) => value.includes("<script>alert(1)</script>"))) {
  throw new Error("unsafe-looking text was not preserved as text");
}
console.log(JSON.stringify({ status: "passed", tags }));
