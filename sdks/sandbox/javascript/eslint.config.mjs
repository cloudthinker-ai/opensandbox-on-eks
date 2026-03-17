import path from "node:path";
import { fileURLToPath } from "node:url";
import { createBaseConfig } from "../../eslint.base.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default createBaseConfig({
  tsconfigRootDir: __dirname,
  tsconfigPath: "./tsconfig.json",
  extraIgnores: ["src/api/**", "src/**/*.d.ts", "src/**/*.js"],
  includeScripts: true,
});
