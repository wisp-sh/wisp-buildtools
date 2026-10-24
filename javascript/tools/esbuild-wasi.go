// Package main bundles JavaScript and TypeScript inside a WASI guest.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"github.com/evanw/esbuild/pkg/api"
)

func main() {
	entry := flag.String("entry", "", "absolute guest path to the entry module")
	output := flag.String("out", "", "absolute guest path to the JavaScript output")
	metafile := flag.String("metafile", "", "absolute guest path to the input manifest")
	version := flag.Bool("version", false, "print compiler version")
	flag.Parse()
	if *version {
		fmt.Println("wisp-esbuild-wasi 0.28.2")
		return
	}
	if *entry == "" || *output == "" || *metafile == "" || flag.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "expected --entry PATH --out PATH --metafile PATH")
		os.Exit(2)
	}
	inputDigests := make(map[string]string)
	var inputMu sync.Mutex
	result := api.Build(api.BuildOptions{
		AbsWorkingDir: "/",
		EntryPoints:   []string{*entry},
		Outfile:       *output,
		Bundle:        true,
		Write:         true,
		Metafile:      true,
		Format:        api.FormatESModule,
		Platform:      api.PlatformNeutral,
		Target:        api.ES2022,
		MainFields:    []string{"module", "main"},
		Conditions:    []string{"import", "module", "default"},
		Charset:       api.CharsetUTF8,
		LogLevel:      api.LogLevelSilent,
		Plugins: []api.Plugin{{
			Name: "wit-imports",
			Setup: func(build api.PluginBuild) {
				build.OnResolve(api.OnResolveOptions{Filter: "^[a-zA-Z][a-zA-Z0-9+.-]*:"},
					func(args api.OnResolveArgs) (api.OnResolveResult, error) {
						return api.OnResolveResult{Path: args.Path, External: true}, nil
					})
				build.OnLoad(api.OnLoadOptions{Filter: ".*", Namespace: "file"},
					func(args api.OnLoadArgs) (api.OnLoadResult, error) {
						bytes, err := os.ReadFile(args.Path)
						if err != nil {
							return api.OnLoadResult{}, err
						}
						contents := string(bytes)
						sum := sha256.Sum256(bytes)
						digest := hex.EncodeToString(sum[:])
						path := filepath.Clean(args.Path)
						inputMu.Lock()
						previous, seen := inputDigests[path]
						inputDigests[path] = digest
						inputMu.Unlock()
						if seen && previous != digest {
							return api.OnLoadResult{}, fmt.Errorf("input changed during compilation: %s", path)
						}
						// LoaderDefault retains upstream's extension-to-loader map
						// for JS/MJS/CJS, TS/MTS/CTS, JSX/TSX, JSON and other
						// supported file types. Hash exactly the returned bytes.
						return api.OnLoadResult{Contents: &contents, Loader: api.LoaderDefault,
							ResolveDir: filepath.Dir(args.Path)}, nil
					})
			},
		}},
	})
	for _, message := range api.FormatMessages(result.Errors, api.FormatMessagesOptions{
		Kind: api.ErrorMessage, Color: false,
	}) {
		fmt.Fprint(os.Stderr, message)
	}
	if len(result.Errors) != 0 {
		os.Exit(1)
	}
	// Parse before publication so a guest or version mismatch cannot produce a
	// successful build with malformed dependency metadata.
	var manifest map[string]interface{}
	if err := json.Unmarshal([]byte(result.Metafile), &manifest); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	manifest["wispInputSha256"] = inputDigests
	metadata, err := json.Marshal(manifest)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := os.WriteFile(*metafile, metadata, 0o600); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
