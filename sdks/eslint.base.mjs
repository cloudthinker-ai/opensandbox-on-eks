import js from "@eslint/js";
import tseslint from "typescript-eslint";
import globals from "globals";

export function createBaseConfig({
  tsconfigRootDir,
  tsconfigPath = "./tsconfig.json",
  extraIgnores = [],
  includeScripts = false,
  scriptGlobs = ["scripts/**/*.{js,mjs,cjs}"],
} = {}) {
  const ignores = ["dist/**", "node_modules/**", "coverage/**", ...extraIgnores];

  const configs = [
    { ignores },
    js.configs.recommended,
    ...tseslint.configs.recommended,
    {
      files: ["src/**/*.{ts,mts,cts}"],
      languageOptions: {
        globals: {
          ...globals.nodeBuiltin,
          ...globals.node,
        },
        parserOptions: {
          project: [tsconfigPath],
          tsconfigRootDir,
        },
      },
      extends: [
        ...tseslint.configs.stylisticTypeChecked,
      ],
      rules: {
        "@typescript-eslint/no-explicit-any": "off",
        "@typescript-eslint/no-unused-vars": [
          "error",
          { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
        ],
        "no-console": "warn",
        "no-debugger": "error",
        "no-constant-condition": "warn",
      },
    },
  ];

  if (includeScripts) {
    configs.push({
      files: scriptGlobs,
      languageOptions: {
        globals: {
          ...globals.nodeBuiltin,
          ...globals.node,
        },
      },
      rules: {
        "no-console": "off",
      },
    });
  }

  return tseslint.config(...configs);
}
