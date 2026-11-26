Typed stubs for the third-party packages resume-kit's scripts use. The packages themselves are
installed per run by uv from each script's `# /// script` header, so they're absent when pyright
runs; that's why `pyrightconfig.json` turns off `reportMissingModuleSource` for
`plugins/resume-kit/scripts` only. Keep each stub to what the scripts call.
