# firmable-gtm-client (TypeScript)

The shared TypeScript library behind every gtm-starter-kit recipe. It mirrors the Python library module for module and is linked to recipes through npm workspaces. It is not published to npm.

It uses Node's built-in `fetch` with `redirect: "manual"` and `AbortSignal.timeout`, and has no runtime dependencies.

```bash
# from the repository root
npm ci
npm run build
node --test "shared/typescript/dist/tests/**/*.test.js"
```

See [shared/python/README.md](../python/README.md) for what each module does; the TypeScript files have the same names.
