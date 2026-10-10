package edge

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The shipped roster has to be valid. If it is not, every consumer is working
// from a table that lies, and a test is the only thing that says so at build
// time rather than 100 seconds into a Kaggle run.
func TestShippedRosterValidates(t *testing.T) {
	if err := ValidateRoster(GgufRoster); err != nil {
		t.Fatalf("shipped GgufRoster is invalid:\n%v", err)
	}
}

// The digests are the load-bearing part of the table, so the two facts that make
// them trustworthy are asserted rather than assumed: they are 64 lowercase hex,
// and they are marked verified. A digest that fails this is not "probably fine".
func TestEveryShippedDigestIsMarkedVerified(t *testing.T) {
	verified := 0
	for _, m := range GgufRoster {
		if m.SHA256 == "" {
			t.Errorf("%s has no digest", m.ID)
			continue
		}
		if !isLowerHex64(m.SHA256) {
			t.Errorf("%s digest is not 64 lowercase hex: %q", m.ID, m.SHA256)
		}
		if !m.SHA256Verified {
			t.Errorf("%s has a digest but SHA256Verified is false", m.ID)
		}
		if m.SHA256Source == "" {
			t.Errorf("%s has a digest with no recorded source", m.ID)
		}
		verified++
	}
	if verified != len(GgufRoster) {
		t.Fatalf("only %d of %d roster entries carry a digest; this repo has no "+
			"network at build time, so every blank one must be a deliberate "+
			"decision and not an oversight", verified, len(GgufRoster))
	}
}

// Both LLM tiers must name the publisher's own conversion, not a community
// quant. A community quant is a third party's post-training of someone else's
// weights, and a translation model that has had its output distribution nudged
// is exactly the kind of thing that shows up as "the subtitles got worse after
// we removed torch" three days later.
func TestLLMRosterUsesTheOfficialConversion(t *testing.T) {
	for _, m := range RosterByTask(GgufTaskLLM) {
		if m.Repo != GgufRepoLLM {
			t.Errorf("%s comes from %q; the LLM must come from %q", m.ID, m.Repo, GgufRepoLLM)
		}
		if !strings.HasSuffix(strings.ToLower(m.File), ".gguf") {
			t.Errorf("%s is %q; the LLM tier must be .gguf", m.ID, m.File)
		}
	}
}

// The ASR rows all have to be the whisper.cpp repo, because that is where the
// ggml conversions live; a faster-whisper / CTranslate2 directory would be a
// torch-shaped model that whisper.cpp cannot load at all.
func TestASRRosterUsesWhisperCpp(t *testing.T) {
	models := RosterByTask(GgufTaskASR)
	if len(models) == 0 {
		t.Fatal("no ASR models in the roster")
	}
	for _, m := range models {
		if m.Repo != GgufRepoASR {
			t.Errorf("%s comes from %q, want %q", m.ID, m.Repo, GgufRepoASR)
		}
		if !strings.HasPrefix(m.File, "ggml-") || !strings.HasSuffix(m.File, ".bin") {
			t.Errorf("%s is %q; whisper.cpp models are named ggml-*.bin", m.ID, m.File)
		}
	}
}

// The two defaults are the whole point of DefaultGguf: a caller that says "give
// me the llm" must get the tier the publisher recommends, not an arbitrary row.
func TestDefaults(t *testing.T) {
	llm, err := DefaultGguf(GgufTaskLLM)
	if err != nil {
		t.Fatalf("DefaultGguf(llm): %v", err)
	}
	if llm.Quant != "Q4_K_M" {
		t.Errorf("default llm is %s, want Q4_K_M", llm.Quant)
	}
	if llm.ID != "homura-2b-q4_k_m" {
		t.Errorf("default llm id is %q", llm.ID)
	}

	asr, err := DefaultGguf(GgufTaskASR)
	if err != nil {
		t.Fatalf("DefaultGguf(asr): %v", err)
	}
	if asr.ID != "whisper-large-v3-turbo" {
		t.Errorf("default asr is %q, want whisper-large-v3-turbo", asr.ID)
	}

	if _, err := DefaultGguf(GgufTask("tts")); err == nil {
		t.Error("DefaultGguf on an unknown task should fail, not guess")
	}
}

func TestLookupGguf(t *testing.T) {
	m, ok := LookupGguf(GgufTaskASR, "whisper-base")
	if !ok {
		t.Fatal("whisper-base not found")
	}
	if m.Repo != GgufRepoASR {
		t.Errorf("repo = %q", m.Repo)
	}

	// An empty task searches everything: a caller holding a bare id should not
	// have to know which lane it lives in.
	if _, ok := LookupGguf("", "whisper-base"); !ok {
		t.Error("unscoped lookup failed")
	}

	// A task filter is a filter, and getting the distinction wrong turns "wrong
	// stage" into "model does not exist" -- which sends the reader to the wrong
	// part of the table entirely.
	if _, ok := LookupGguf(GgufTaskLLM, "whisper-base"); ok {
		t.Error("an ASR model resolved for the LLM task")
	}
	if _, ok := LookupGguf("", "no-such-model"); ok {
		t.Error("a bogus id resolved")
	}
	if _, ok := LookupGguf("", ""); ok {
		t.Error("an empty id resolved")
	}
}

// Roster hands out copies. Sorting a returned slice in place must not reorder
// the package table, or one caller's sort silently changes everyone's lookup
// order on the next run.
func TestRosterReturnsCopies(t *testing.T) {
	before := GgufRoster[0].ID
	got := Roster()
	for i := range got {
		got[i].ID = "clobbered"
	}
	if GgufRoster[0].ID != before {
		t.Fatalf("Roster() exposed package state: GgufRoster[0].ID changed from %q", before)
	}

	// Sorted by task then id, so the Python table and the Go log agree.
	sorted := Roster()
	for i := 1; i < len(sorted); i++ {
		a, b := sorted[i-1], sorted[i]
		if a.Task > b.Task || (a.Task == b.Task && a.ID > b.ID) {
			t.Fatalf("Roster() is not sorted at %d: %v/%v then %v/%v", i, a.Task, a.ID, b.Task, b.ID)
		}
	}
}

func TestValidRosterRejectsBadTables(t *testing.T) {
	good := GgufRoster[0]

	cases := []struct {
		name   string
		mutate func([]GgufModel) []GgufModel
		want   string
	}{
		{
			name:   "duplicate id",
			mutate: func(m []GgufModel) []GgufModel { return append(m, m[0]) },
			want:   "duplicate id",
		},
		{
			name: "repo without an org",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].Repo = "Index-Homura-2B-GGUF"
				return m
			},
			want: "org/name",
		},
		{
			name: "repo with a trailing slash",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].Repo = "IndexTeam/Index-Homura-2B-GGUF/"
				return m
			},
			want: "empty path segment",
		},
		{
			name: "repo with two slashes",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].Repo = "a/b/c"
				return m
			},
			want: "org/name",
		},
		{
			name: "file with a directory component",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].File = "Q4_K_M/Index-Homura-2B.gguf"
				return m
			},
			want: "bare filename",
		},
		{
			name: "file with a parent traversal",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].File = "../secret.gguf"
				return m
			},
			want: "bare filename",
		},
		{
			name: "file with a NUL",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].File = "ok\x00.gguf"
				return m
			},
			want: "NUL",
		},
		{
			name: "torch weights sneaking into the roster",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].File = "model.safetensors"
				return m
			},
			want: ".gguf or .bin",
		},
		{
			name: "unknown task",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].Task = GgufTask("tts")
				return m
			},
			want: "task must be",
		},
		{
			name: "digest that is not 64 hex",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].SHA256 = "deadbeef"
				return m
			},
			want: "64 lowercase hex",
		},
		{
			name: "uppercase digest",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].SHA256 = strings.ToUpper(m[0].SHA256)
				return m
			},
			want: "64 lowercase hex",
		},
		{
			name: "digest present but not marked verified",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].SHA256Verified = false
				return m
			},
			want: "sha256_verified is false",
		},
		{
			name: "marked verified with no digest",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].SHA256 = ""
				return m
			},
			want: "sha256_verified is true but no sha256",
		},
		{
			name: "digest with no recorded source",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].SHA256Source = ""
				return m
			},
			want: "sha256_source",
		},
		{
			name: "negative size",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].SizeBytes = -1
				return m
			},
			want: "negative size_bytes",
		},
		{
			name: "no context window on a real model",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].CtxTokens = 0
				return m
			},
			want: "ctx_tokens must be > 0",
		},
		{
			name: "id with a slash",
			mutate: func(m []GgufModel) []GgufModel {
				m[0].ID = "org/model"
				return m
			},
			want: "lowercase",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// Copy the whole shipped table so a mutation cannot reach package
			// state through the slice header.
			clone := append([]GgufModel(nil), GgufRoster...)
			got := tc.mutate(clone)
			err := ValidateRoster(got)
			if err == nil {
				t.Fatalf("ValidateRoster accepted a table with %s", tc.name)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error does not mention %q:\n%v", tc.want, err)
			}
		})
	}

	// And the control: the untouched table must pass, otherwise "the error
	// mentioned the right word" is worth nothing.
	if err := ValidateRoster(append([]GgufModel(nil), GgufRoster...)); err != nil {
		t.Fatalf("a copied, unmutated roster failed validation: %v", err)
	}
	_ = good
}

// Every problem is reported, not just the first. A roster is hand-edited, and
// one-at-a-time reporting turns five typos into five edit-build cycles.
func TestValidateRosterReportsEveryProblem(t *testing.T) {
	bad := []GgufModel{
		{ID: "a", Repo: "noorg", File: "a.txt", Task: GgufTaskLLM},
		{ID: "b", Repo: "o/n", File: "b.safetensors", Task: GgufTask("nope")},
	}
	err := ValidateRoster(bad)
	if err == nil {
		t.Fatal("expected an error")
	}
	msg := err.Error()
	for _, want := range []string{"org/name", ".gguf or .bin", "task must be"} {
		if !strings.Contains(msg, want) {
			t.Errorf("missing %q in:\n%v", want, msg)
		}
	}
}

// A projector is allowed to have no context window; nothing else is. If this
// ever stops holding, the exemption has leaked.
func TestProjectorExemptionIsNarrow(t *testing.T) {
	proj := GgufModel{
		ID: "proj", Repo: "o/n", File: "m.mmproj-Q8_0.gguf",
		Quant: "Q8_0", Task: GgufTaskLLM, SHA256Verified: false,
	}
	if err := ValidateRoster(append(append([]GgufModel(nil), GgufRoster...), proj)); err != nil {
		t.Fatalf("a projector with ctx_tokens 0 was rejected: %v", err)
	}

	named := proj
	named.File = "m.gguf"
	if err := ValidateRoster(append(append([]GgufModel(nil), GgufRoster...), named)); err == nil {
		t.Fatal("a non-projector file with ctx_tokens 0 was accepted")
	}
}

// The Python resolver needs the same table. It lives in huggingface/ so the
// Kaggle worker can read it without a Go build, which means it can drift -- so
// drift is a test failure rather than a runtime surprise where one end verifies
// a digest the other end never agreed to.
func TestPythonRosterMirrorMatchesGo(t *testing.T) {
	path := filepath.Join("..", "huggingface", "models_gguf.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			t.Skipf("no mirror at %s", path)
		}
		t.Fatalf("read %s: %v", path, err)
	}

	var doc struct {
		Version int `json:"version"`
		Models  []struct {
			ID             string `json:"id"`
			Repo           string `json:"repo"`
			File           string `json:"file"`
			Quant          string `json:"quant"`
			Task           string `json:"task"`
			SHA256         string `json:"sha256"`
			SHA256Verified bool   `json:"sha256_verified"`
			SizeBytes      int64  `json:"size_bytes"`
			CtxTokens      int    `json:"ctx_tokens"`
		} `json:"models"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	if doc.Version != 1 {
		t.Fatalf("mirror version %d, this build speaks 1", doc.Version)
	}
	if len(doc.Models) != len(GgufRoster) {
		t.Fatalf("mirror has %d models, Go roster has %d", len(doc.Models), len(GgufRoster))
	}

	for _, py := range doc.Models {
		go1, ok := LookupGguf("", py.ID)
		if !ok {
			t.Errorf("mirror has %q, Go roster does not", py.ID)
			continue
		}
		if go1.Task != GgufTask(py.Task) {
			t.Errorf("%s: task %q vs %q", py.ID, py.Task, go1.Task)
		}
		if go1.Repo != py.Repo {
			t.Errorf("%s: repo %q vs %q", py.ID, py.Repo, go1.Repo)
		}
		if go1.File != py.File {
			t.Errorf("%s: file %q vs %q", py.ID, py.File, go1.File)
		}
		if go1.Quant != py.Quant {
			t.Errorf("%s: quant %q vs %q", py.ID, py.Quant, go1.Quant)
		}
		// The comparison that actually matters. Everything else is a cosmetic
		// drift; a wrong digest is a model that never verifies again.
		if !strings.EqualFold(go1.SHA256, py.SHA256) {
			t.Errorf("%s: DIGEST MISMATCH go=%q python=%q", py.ID, go1.SHA256, py.SHA256)
		}
		if go1.SHA256Verified != py.SHA256Verified {
			t.Errorf("%s: sha256_verified %v vs %v", py.ID, go1.SHA256Verified, py.SHA256Verified)
		}
		if go1.SizeBytes != py.SizeBytes {
			t.Errorf("%s: size_bytes %d vs %d", py.ID, py.SizeBytes, go1.SizeBytes)
		}
		if go1.CtxTokens != py.CtxTokens {
			t.Errorf("%s: ctx_tokens %d vs %d", py.ID, py.CtxTokens, go1.CtxTokens)
		}
	}
}

// The URL builders are duplicated on the Python side; if either drifts, the
// client silently falls through to the hub and the "0 s instead of 525 s" claim
// quietly becomes false again -- the exact failure mode of the original
// /artefacts/ bug. So the shapes are pinned here.
func TestURLShapes(t *testing.T) {
	m := GgufModel{Repo: "IndexTeam/Index-Homura-2B-GGUF", File: "Index-Homura-2B.Q4_K_M.gguf"}

	if got, want := m.GgufHTTPPath(), "/gguf/Index-Homura-2B.Q4_K_M.gguf"; got != want {
		t.Errorf("GgufHTTPPath = %q, want %q", got, want)
	}
	if got, want := m.EdgeArtefactName(), "gguf/Index-Homura-2B.Q4_K_M.gguf"; got != want {
		t.Errorf("EdgeArtefactName = %q, want %q", got, want)
	}
	if got, want := m.HubURL(""), "https://huggingface.co/IndexTeam/Index-Homura-2B-GGUF/resolve/main/Index-Homura-2B.Q4_K_M.gguf"; got != want {
		t.Errorf("HubURL = %q, want %q", got, want)
	}
	if got, want := m.HubURL("abc123"), "https://huggingface.co/IndexTeam/Index-Homura-2B-GGUF/resolve/abc123/Index-Homura-2B.Q4_K_M.gguf"; got != want {
		t.Errorf("HubURL(rev) = %q, want %q", got, want)
	}
}
