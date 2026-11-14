# TypeScript 7 `typescript-eslint` qualification — blocked

This is blocker evidence for #127. It intentionally makes no dependency or vendor-source change. PR #241 owns the TypeScript 7.0.2 dependency bump; this qualification cannot provide the required ESLint prerequisite yet.

## Result

No compatible released `typescript-eslint` combination was found for TypeScript 7.0.2.

The newest stable release is `8.66.0`. Its packages still declare a TypeScript peer range ending before 6.1.0, and bypassing peer resolution does not make it work:

- `typescript-eslint@8.66.0` and `@typescript-eslint/parser@8.66.0` abort with:

  ```text
  typescript-eslint does not support TS 7.0.
  ```

- Directly invoking `@typescript-eslint/typescript-estree@8.66.0` fails while loading the removed TypeScript API:

  ```text
  TypeError: Cannot read properties of undefined (reading 'Cjs')
      at .../@typescript-eslint/typescript-estree/dist/create-program/shared.js:59:18
  ```

The newest published canary (`8.66.1-alpha.8`) has the same peer range, runtime guard, and `ts.Extension.Cjs` failure.

## Registry checks

Commands were run from the repository workspace:

```text
npm view typescript-eslint@latest version peerDependencies --json
npm view @typescript-eslint/typescript-estree@latest version peerDependencies --json
```

Results:

```json
// typescript-eslint@latest
{
  "version": "8.66.0",
  "peerDependencies": {
    "eslint": "^8.57.0 || ^9.0.0 || ^10.0.0",
    "typescript": ">=4.8.4 <6.1.0"
  }
}

// @typescript-eslint/typescript-estree@latest
{
  "version": "8.66.0",
  "peerDependencies": {
    "typescript": ">=4.8.4 <6.1.0"
  }
}
```

The latest parser and plugin are also `8.66.0`; both declare `typescript >=4.8.4 <6.1.0`. The plugin additionally requires `@typescript-eslint/parser ^8.66.0`.

The npm `canary` tag points to `typescript-eslint@8.66.1-alpha.8`; its wrapper, parser, plugin, and `typescript-estree` packages retain the same TypeScript peer ceiling.

## Empirical qualification

All tests used TypeScript `7.0.2`, ESLint `10.8.0`, and the frontend source in `itambox/static/src`.

### Normal dependency resolution

```text
npm install --save-dev typescript@7.0.2 typescript-eslint@8.66.0 \
  @typescript-eslint/parser@8.66.0 @typescript-eslint/eslint-plugin@8.66.0
```

Fails with `ERESOLVE` because `@typescript-eslint/parser@8.66.0` requires `typescript >=4.8.4 <6.1.0`.

### Version matrix

| Combination | Install mode | `npx eslint static/src` | Failure |
| --- | --- | --- | --- |
| `typescript-eslint` / parser / plugin `8.61.1` | `--legacy-peer-deps` | exit 2 | `@typescript-eslint/typescript-estree` reads `ts.Extension.Cjs`; `TypeError: Cannot read properties of undefined (reading 'Cjs')` at `dist/create-program/shared.js:59:18` |
| `typescript-eslint` / parser / plugin `8.66.0` | `--legacy-peer-deps` | exit 2 | Explicit `typescript-eslint does not support TS 7.0` guard |
| `typescript-eslint` / parser / plugin `8.66.1-alpha.8` | `--legacy-peer-deps` | exit 2 | Explicit `typescript-eslint does not support TS 7.0` guard |

The direct-parser configuration was also tested for `8.66.0`; it hit the same TS 7.0 guard. Direct `typescript-estree` parsing with `8.66.0` and `8.66.1-alpha.8` hit the `ts.Extension.Cjs` failure.

The `--legacy-peer-deps` flag was used only to reach runtime qualification. It is not a viable project installation mode because it accepts the unsupported peer relationship.

## Frontend gates under the newest stable candidate

After installing the `8.66.0` candidate with TypeScript `7.0.2` using `--legacy-peer-deps`:

```text
npm run build:all       PASS (exit 0)
npx tsc --noEmit        PASS (exit 0)
npx eslint static/src   BLOCKED (exit 2)
npm run lint:styles     PASS (exit 0)
```

No vendor patch or workaround was applied. The package manifests were restored unchanged after qualification, so this PR contains documentation only.

## Open question

A future typescript-eslint release must both support the TypeScript 7.0 API and widen its TypeScript peer range before PR #241 can become merge-ready. Upstream tracking referenced by the runtime guard: https://github.com/typescript-eslint/typescript-eslint/issues/10940
