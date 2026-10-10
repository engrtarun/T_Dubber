package edge

// The .gguf roster: the models the PyTorch-Free architecture actually needs.
//
// WHY THIS FILE EXISTS
// --------------------
// Kaggle run test4_gotgVERSION burned 1067s of its 1258s pip-installing vLLM +
// PyTorch + CUDA, then died on:
//
//	ImportError: libcudart.so.13: cannot open shared object file
//
// vLLM 0.26.0 ships CUDA 13 wheels; the Kaggle image ships CUDA 12. There is no
// pin that fixes that from the inside, because the mismatch is between what pip
// wants to install and what the image provides. So the torch stack is going away
// and llama.cpp / whisper.cpp take its place.
//
// That swap is only possible if the controller knows WHICH file to fetch, per
// task, without asking the hub to resolve it at run time. That is this file: a
// static, validated table of repo -> file -> digest.
//
// THE ONE RULE IN HERE: NEVER INVENT A DIGEST
// -------------------------------------------
// A wrong sha256 is strictly worse than a missing one. A missing digest means
// "size-checked only" and the run still works. A wrong digest means every
// future download of that model fails verification forever, the error names a
// digest nobody recognises, and nobody can work out why a published file
// "keeps corrupting". So:
//
//	SHA256 == ""            -> verified == false, and that is the whole story
//	SHA256 != ""           -> 64 lowercase hex chars, verified == true
//
// ValidateRoster enforces both halves. There is no third state.
//
// PROVENANCE OF THE DIGESTS BELOW
// -------------------------------
// Every Homura entry is cross-checked against TWO independent sources that
// agree byte for byte:
//
//  1. the publisher's own SHA256SUMS.txt in IndexTeam/Index-Homura-2B-GGUF
//  2. the git-LFS object id served by the Hugging Face tree API, which for LFS
//     is the sha256 of the file content
//
// The whisper entries come from the LFS object id alone (HF no longer ships the
// digests in download-ggml-model.sh; that script hardcodes none). One
// authoritative machine-readable source rather than two, and SHA256Source says
// so on every row. Nothing here was computed by guessing.

import (
	"errors"
	"fmt"
	"sort"
	"strings"
)

// GgufTask names the pipeline role a model fills. It is a closed set because the
// concurrency rule in queue.go is keyed on it: `llm` is the GPU-0 hotspot and
// `asr` is the GPU-1 hotspot, and an unknown task would silently fall out of
// that guarantee.
type GgufTask string

const (
	// GgufTaskLLM is the translation model llama.cpp serves. Serialised: one
	// LLM at a time, because it owns GPU 0.
	GgufTaskLLM GgufTask = "llm"

	// GgufTaskASR is transcription, run by whisper.cpp. Also serialised against
	// itself, on GPU 1 -- but runs concurrently with `llm`. See queue.go.
	GgufTaskASR GgufTask = "asr"
)

// GgufDirName is the directory under the artefact root that /gguf/<name>
// serves from. A gguf is a single multi-gigabyte file, never an archive, so it
// gets its own flat directory instead of the weights/ role the tarball packs
// used.
const GgufDirName = "gguf"

// GgufSHAHeader carries the digest of a served blob. The client verifies against
// it AFTER the transfer, which is the only order that means anything: a digest
// header sent before the body is a claim about bytes that have not arrived yet.
const GgufSHAHeader = "X-Content-Sha256"

// GgufRepoASR is where every whisper.cpp ggml model lives. One repo for all of
// them, which is why the ASR rows differ only in File.
const GgufRepoASR = "ggerganov/whisper.cpp"

// GgufRepoLLM is the publisher's own GGUF conversion of the translation model
// the vLLM logs named. Official, not a community quant: the card records a
// per-token KL check of Q4_K_M against the F16 conversion on an A100.
const GgufRepoLLM = "IndexTeam/Index-Homura-2B-GGUF"

// GgufModel is one row of the roster.
//
// Quant and SizeBytes are planning data, not identity: SizeBytes is what the
// resolver compares against before it hashes anything, so a wrong value costs
// one wasted hash rather than a wrong install. Identity is ID plus, when it is
// known, SHA256.
type GgufModel struct {
	// ID is the roster key. Stable and hand-readable, because it is what a
	// notebook passes as `--model` and what a log line prints. Never rename one
	// to "tidy" it: the name is in saved datasets.
	ID string `json:"id"`

	// Repo is the Hugging Face repository, "org/name". Together with File it is
	// the hub URL and the provenance of every byte.
	Repo string `json:"repo"`

	// File is the bare filename inside Repo -- "Index-Homura-2B.Q4_K_M.gguf".
	// No directory component: ValidateRoster rejects one, because a path here
	// is either a mistake or an attempt to escape the repo.
	File string `json:"file"`

	// Quant is the quantisation label, "Q4_K_M" or "fp16". It is read from the
	// filename by convention but stored explicitly so a consumer never has to
	// parse one.
	Quant string `json:"quant"`

	// Task is llm or asr. Drives DefaultGguf and the queue's hotspot rule.
	Task GgufTask `json:"task"`

	// SHA256 is the file's content digest, or "" when nobody has confirmed one.
	// Never a placeholder, never a truncation: see the file header.
	SHA256 string `json:"sha256,omitempty"`

	// SHA256Verified says whether SHA256 was read from a publisher-supplied or
	// registry-supplied manifest, as opposed to being absent. It is a separate
	// field because "" and "somebody wrote something here" must not be
	// confusable, and a consumer that requires a verified digest has to be able
	// to say so in one expression.
	SHA256Verified bool `json:"sha256_verified"`

	// SHA256Source records where the digest came from, so a future reader can
	// re-check it and knows how much to trust it. Free text.
	SHA256Source string `json:"sha256_source,omitempty"`

	// SizeBytes is the published byte length. Used as the cheap pre-check before
	// hashing, and for disk planning.
	SizeBytes int64 `json:"size_bytes"`

	// CtxTokens is the context window the model was trained for, in tokens. For
	// ASR it is the whisper mel window (1500 frames = 30 s of audio), not a
	// token count at all -- the column means "how much input this stage can
	// take before it must chunk", which is what the chunker actually asks.
	CtxTokens int `json:"ctx_tokens"`

	// Notes is free text for a human. Consumers must not parse it.
	Notes string `json:"notes,omitempty"`
}

// hubSHA is shorthand for "the Hugging Face LFS object id, which for LFS is the
// sha256 of the file contents".
const hubSHA = "huggingface lfs oid (api/models/<repo>/tree?expand=true)"

// sha256SUMS is shorthand for the publisher's own SHA256SUMS.txt.
const sha256SUMS = "IndexTeam/Index-Homura-2B-GGUF:SHA256SUMS.txt"

// GgufRoster is every .gguf the pipeline knows how to ask for.
//
// Both Homura digest sources were compared and agree on all 15 files; only the
// tiers that could plausibly be selected are listed. f16 is here as the
// reference a quality regression is measured against, not as something to run.
var GgufRoster = []GgufModel{
	{
		ID:             "homura-2b-q4_k_m",
		Repo:           GgufRepoLLM,
		File:           "Index-Homura-2B.Q4_K_M.gguf",
		Quant:          "Q4_K_M",
		Task:           GgufTaskLLM,
		SHA256:         "3fc56945f1db4c2b91b18ac9f4061b354d51c226a6fdd9ea95e72a927f13a675",
		SHA256Verified: true,
		SHA256Source:   sha256SUMS + " + " + hubSHA,
		SizeBytes:      1312164480,
		CtxTokens:      262144,
		Notes: "DEFAULT llm. The card calls Q4_K_M the recommended tier and reports " +
			"greedy output matching the BF16 reference almost verbatim. 1.31 GB -- " +
			"a fifth of the vLLM+torch download it replaces. Serve with " +
			"`llama serve -hf IndexTeam/Index-Homura-2B-GGUF:Q4_K_M`. CtxTokens is the " +
			"trained maximum (262144); the worker passes a far smaller -c because a " +
			"dubbing chunk is a few hundred tokens.",
	},
	{
		ID:             "homura-2b-q5_k_m",
		Repo:           GgufRepoLLM,
		File:           "Index-Homura-2B.Q5_K_M.gguf",
		Quant:          "Q5_K_M",
		Task:           GgufTaskLLM,
		SHA256:         "3f206ccb12c3d753ad132c22f6360a842de7a0849308a69c6f43355bee91fd20",
		SHA256Verified: true,
		SHA256Source:   sha256SUMS + " + " + hubSHA,
		SizeBytes:      1454787200,
		CtxTokens:      262144,
		Notes: "Fallback when a translation quality regression shows up. Costs 142 MB " +
			"over Q4_K_M and buys back almost nothing per the publisher's KL check.",
	},
	{
		ID:             "homura-2b-q8_0",
		Repo:           GgufRepoLLM,
		File:           "Index-Homura-2B.Q8_0.gguf",
		Quant:          "Q8_0",
		Task:           GgufTaskLLM,
		SHA256:         "e3aa4850c1f4779328af5d721383bb5785dfdd7836f5a82976336c21178c7754",
		SHA256Verified: true,
		SHA256Source:   sha256SUMS + " + " + hubSHA,
		SizeBytes:      2076674688,
		CtxTokens:      262144,
		Notes: "Near-lossless. Only worth the extra 764 MB over Q4_K_M when a " +
			"terminology-critical dub is being re-run for quality, not for throughput.",
	},
	{
		ID:             "homura-2b-f16",
		Repo:           GgufRepoLLM,
		File:           "Index-Homura-2B.f16.gguf",
		Quant:          "f16",
		Task:           GgufTaskLLM,
		SHA256:         "4d71154c3ac68699138822ef4373071b08ac615bbdcaaa758c45490488514bac",
		SHA256Verified: true,
		SHA256Source:   sha256SUMS + " + " + hubSHA,
		SizeBytes:      3897387648,
		CtxTokens:      262144,
		Notes: "Lossless conversion baseline. Not a run target -- it is what the " +
			"quantised tiers were validated against, so it has to stay fetchable for a " +
			"regression to mean anything.",
	},
	{
		ID:             "homura-2b-mmproj-q8_0",
		Repo:           GgufRepoLLM,
		File:           "Index-Homura-2B.mmproj-Q8_0.gguf",
		Quant:          "Q8_0",
		Task:           GgufTaskLLM,
		SHA256:         "de0f4f784b4437d84baab7ed2e0ead657070eadf165da97cc0df63ad9fdb677d",
		SHA256Verified: true,
		SHA256Source:   sha256SUMS + " + " + hubSHA,
		SizeBytes:      361518336,
		CtxTokens:      0,
		Notes: "Vision projector. Only needed by llama-mtmd-cli for image input; the " +
			"dubbing pipeline is text-only, so this ships with an empty download of the " +
			"job by default. CtxTokens is 0 because a projector has no context window, " +
			"and ValidateRoster allows that only for a mmproj- file.",
	},
	{
		ID:             "whisper-large-v3-turbo",
		Repo:           GgufRepoASR,
		File:           "ggml-large-v3-turbo.bin",
		Quant:          "fp16",
		Task:           GgufTaskASR,
		SHA256:         "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
		SHA256Verified: true,
		SHA256Source:   hubSHA,
		SizeBytes:      1624555275,
		CtxTokens:      1500,
		Notes: "DEFAULT asr. Replaces Systran/faster-whisper-large-v3 (CTranslate2). " +
			"8.0 decoder layers instead of 32, which is where the speed comes from -- " +
			"not a smaller encoder. CtxTokens 1500 is the whisper mel window: 30 s of " +
			"audio, so the chunker cuts here regardless of chunk length.",
	},
	{
		ID:             "whisper-base",
		Repo:           GgufRepoASR,
		File:           "ggml-base.bin",
		Quant:          "fp16",
		Task:           GgufTaskASR,
		SHA256:         "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
		SHA256Verified: true,
		SHA256Source:   hubSHA,
		SizeBytes:      147951465,
		CtxTokens:      1500,
		Notes: "144 MB, for a tight-VRAM run or a smoke test. Materially worse WER " +
			"than turbo on proper nouns, which is most of what a dub needs.",
	},
	{
		ID:             "whisper-small",
		Repo:           GgufRepoASR,
		File:           "ggml-small.bin",
		Quant:          "fp16",
		Task:           GgufTaskASR,
		SHA256:         "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
		SHA256Verified: true,
		SHA256Source:   hubSHA,
		SizeBytes:      487601967,
		CtxTokens:      1500,
		Notes: "Middle tier. Only useful when turbo does not fit and base is not " +
			"accurate enough; the 340 MB over base buys a large accuracy gap, the 1.1 GB " +
			"over turbo buys very little.",
	},
}

// ggufDefaults is which row each task resolves to when the caller names a task
// but no model.
//
// It is a separate map rather than a bool on the struct because "is the default"
// is a property of the SET, not of a model: two rows both flagged default is a
// roster bug that a per-row flag cannot express, and ValidateRoster checks for
// exactly that.
var ggufDefaults = map[GgufTask]string{
	GgufTaskLLM: "homura-2b-q4_k_m",
	GgufTaskASR: "whisper-large-v3-turbo",
}

// Errors returned by the lookup helpers. Callers distinguish "no such model" from
// "that model exists but its digest is unknown", because the second is usable
// with weaker checks and the first is not usable at all.
var (
	// ErrNoGguf means the roster has no entry with that id (or none for that
	// task when the id is empty).
	ErrNoGguf = errors.New("edge: no such gguf model")

	// ErrNoDefaultGguf means the task is valid but no default is registered for
	// it -- a roster and a defaults map that have drifted apart.
	ErrNoDefaultGguf = errors.New("edge: no default gguf model for task")
)

// DefaultGguf returns the model a task uses when the caller does not name one.
//
// The choice is not arbitrary and is worth stating: Q4_K_M for llm because the
// publisher validated it against the F16 conversion and the run is throughput
// bound, not accuracy bound; large-v3-turbo for asr because dropping 24 of 32
// decoder layers is a real speedup at no accuracy cost that matters for dubbing.
func DefaultGguf(task GgufTask) (GgufModel, error) {
	id, ok := ggufDefaults[task]
	if !ok {
		return GgufModel{}, fmt.Errorf("%w: %q", ErrNoDefaultGguf, task)
	}
	m, ok := LookupGguf(task, id)
	if !ok {
		return GgufModel{}, fmt.Errorf("%w: default %q for task %q", ErrNoGguf, id, task)
	}
	return m, nil
}

// LookupGguf finds a model by id, optionally scoped to a task.
//
// An empty task searches the whole roster, which is what a caller holding a bare
// id string wants. A non-empty task is a filter, so LookupGguf("asr", "whisper-base")
// finds it and LookupGguf("llm", "whisper-base") does not -- which is the
// difference between "not in the roster" and "not usable for this stage", and
// reporting them as the same thing sends people looking in the wrong place.
//
// It returns a copy. GgufRoster is package state and a caller that sorted it in
// place would corrupt the table for everyone else.
func LookupGguf(task GgufTask, id string) (GgufModel, bool) {
	if id == "" {
		return GgufModel{}, false
	}
	for _, m := range GgufRoster {
		if m.ID != id {
			continue
		}
		if task != "" && m.Task != task {
			continue
		}
		return m, true
	}
	return GgufModel{}, false
}

// Roster returns every model, ordered by task then id, as copies.
//
// Sorted, because the Python side renders this as a table and a roster that
// reorders itself between runs produces diffs nobody can read. A copy, for the
// reason on LookupGguf.
func Roster() []GgufModel {
	out := make([]GgufModel, len(GgufRoster))
	copy(out, GgufRoster)
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Task != out[j].Task {
			return out[i].Task < out[j].Task
		}
		return out[i].ID < out[j].ID
	})
	return out
}

// RosterByTask returns the models for one task, in id order.
func RosterByTask(task GgufTask) []GgufModel {
	out := []GgufModel{}
	for _, m := range Roster() {
		if m.Task == task {
			out = append(out, m)
		}
	}
	return out
}

// ValidateRoster checks a table and returns every problem it finds, not just the
// first. A roster is edited by hand, and one-at-a-time fixing means five
// edit-build cycles for five typos.
//
// The checks, and what each one is really preventing:
//
//   - duplicate ID        -- two rows, one key, a silent resolution coin-flip
//   - malformed Repo      -- a "repo" with no org is a typo, and a repo with a
//     trailing slash or two slashes would build a URL that 404s at fetch time,
//     long after the mistake
//   - malformed File      -- a path separator in File is either a mistake or an
//     attempt to make the fetcher read outside the repo
//   - unknown Task        -- falls out of the queue's hotspot guarantee
//   - digest shape        -- 64 lowercase hex, or nothing
//   - digest/verified     -- disagreement in EITHER direction is the bug this
//     whole file exists to prevent: a digest with no verified flag will be
//     trusted, and a verified flag with no digest is a lie a caller acts on
//   - negative SizeBytes  -- the resolver compares against it before hashing
//   - CtxTokens           -- required except on a projector file, which has no
//     context window to speak of
func ValidateRoster(models []GgufModel) error {
	var problems []string

	seenID := map[string]int{}
	for i, m := range models {
		where := fmt.Sprintf("models[%d] %q", i, m.ID)

		if m.ID == "" {
			problems = append(problems, where+": empty id")
		} else if !validRosterID(m.ID) {
			problems = append(problems, fmt.Sprintf("%s: id must be lowercase [a-z0-9._-], got %q", where, m.ID))
		} else if first, dup := seenID[m.ID]; dup {
			// The first index is named because "duplicate id" alone sends the
			// reader hunting for the other copy of an id they have not seen.
			problems = append(problems, fmt.Sprintf("%s: duplicate id, already used by models[%d]", where, first))
		} else {
			seenID[m.ID] = i
		}

		if err := validHFRepo(m.Repo); err != nil {
			problems = append(problems, where+": "+err.Error())
		}
		if err := validGGUFFile(m.File); err != nil {
			problems = append(problems, where+": "+err.Error())
		}

		switch m.Task {
		case GgufTaskLLM, GgufTaskASR:
		case "":
			problems = append(problems, where+": empty task")
		default:
			problems = append(problems, fmt.Sprintf("%s: task must be %q or %q, got %q",
				where, GgufTaskLLM, GgufTaskASR, m.Task))
		}

		if m.Quant == "" {
			problems = append(problems, where+": empty quant")
		}
		if m.SizeBytes < 0 {
			problems = append(problems, fmt.Sprintf("%s: negative size_bytes %d", where, m.SizeBytes))
		}
		// A projector is a vision adapter with no prompt surface, so demanding a
		// context window of it would mean writing a meaningless number into the
		// table. Every other file must have one: a chunker that cannot read it
		// will silently fall back to a default.
		if m.CtxTokens <= 0 && !strings.Contains(strings.ToLower(m.File), "mmproj") {
			problems = append(problems, fmt.Sprintf("%s: ctx_tokens must be > 0 for a non-projector file", where))
		}

		switch {
		case m.SHA256 == "":
			if m.SHA256Verified {
				problems = append(problems, where+": sha256_verified is true but no sha256 is present -- "+
					"a caller would treat an absent digest as verified")
			}
		case !isLowerHex64(m.SHA256):
			problems = append(problems, fmt.Sprintf("%s: sha256 must be 64 lowercase hex chars, got %q", where, m.SHA256))
		default:
			if !m.SHA256Verified {
				problems = append(problems, where+": sha256 is present but sha256_verified is false -- "+
					"an unverified digest will be trusted, which is the failure this table exists to prevent")
			}
			if m.SHA256Source == "" {
				problems = append(problems, where+": sha256 present with no sha256_source -- "+
					"nobody can re-check a digest whose provenance is unrecorded")
			}
		}
	}

	for task, id := range ggufDefaults {
		found := false
		for _, m := range models {
			if m.ID == id && m.Task == task {
				found = true
				break
			}
		}
		if !found {
			problems = append(problems, fmt.Sprintf("default for task %q names %q, which is not in this table", task, id))
		}
	}

	if len(problems) > 0 {
		return fmt.Errorf("edge: invalid gguf roster:\n  %s", strings.Join(problems, "\n  "))
	}
	return nil
}

func validRosterID(id string) bool {
	for i := 0; i < len(id); i++ {
		c := id[i]
		switch {
		case c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '.', c == '_', c == '-':
		default:
			return false
		}
	}
	return id != ""
}

// validHFRepo enforces "org/name" with nothing else in it.
//
// Deliberately stricter than "has a slash". A trailing slash, a double slash
// and a bare name all produce a URL that fails at fetch time -- that is 100
// seconds into a Kaggle run, and the error names a network endpoint rather than
// a typo in a table.
func validHFRepo(repo string) error {
	if repo == "" {
		return errors.New("empty repo")
	}
	if strings.ContainsAny(repo, " \\") {
		return fmt.Errorf("repo %q contains whitespace or a backslash", repo)
	}
	// A trailing slash, a leading slash and a double slash all put an empty
	// segment in the URL -- and the failure shows up far from here, as a
	// 404 on the hub at fetch time. Named explicitly so the reader does not
	// have to parse "got 3 path segments" to learn which segment was empty.
	if strings.HasPrefix(repo, "/") || strings.HasSuffix(repo, "/") || strings.Contains(repo, "//") {
		return fmt.Errorf("repo %q has an empty path segment", repo)
	}
	parts := strings.Split(repo, "/")
	if len(parts) != 2 {
		return fmt.Errorf("repo %q must be exactly org/name (got %d path segments)", repo, len(parts))
	}
	for _, seg := range parts {
		if seg == "" {
			return fmt.Errorf("repo %q has an empty path segment", repo)
		}
		for i := 0; i < len(seg); i++ {
			c := seg[i]
			switch {
			case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9', c == '.', c == '_', c == '-':
			default:
				return fmt.Errorf("repo %q contains %q, which is not allowed in a hub repo id", repo, string(c))
			}
		}
	}
	return nil
}

// validGGUFFile requires a bare filename ending in a quantiser suffix this
// pipeline knows how to load.
//
// ".bin" is accepted for the whisper ggml models, which predate the .gguf
// extension. ".safetensors" is deliberately NOT accepted: if a torch weight
// ends up in this table the PyTorch-free architecture has failed, and the
// correct outcome is a validation error here rather than a 5 GB download and a
// crash on the GPU.
func validGGUFFile(name string) error {
	if name == "" {
		return errors.New("empty file")
	}
	if strings.ContainsAny(name, "/\\") {
		return fmt.Errorf("file %q must be a bare filename with no directory component", name)
	}
	if name == "." || name == ".." {
		return fmt.Errorf("file %q is not a filename", name)
	}
	if strings.ContainsRune(name, 0) {
		return fmt.Errorf("file %q contains NUL", name)
	}
	lower := strings.ToLower(name)
	if !strings.HasSuffix(lower, ".gguf") && !strings.HasSuffix(lower, ".bin") {
		return fmt.Errorf("file %q must end in .gguf or .bin", name)
	}
	if len(name) > 200 {
		return fmt.Errorf("file name %q is implausibly long", name)
	}
	return nil
}

func isLowerHex64(s string) bool {
	if len(s) != 64 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= '0' && c <= '9', c >= 'a' && c <= 'f':
		default:
			return false
		}
	}
	return true
}

// GgufHTTPPath is the route a client GETs for this model, relative to the Space
// root. It lives here rather than in the server because the Python resolver
// needs the exact same string and two copies of a URL is how the `/artefacts/`
// vs `/artifact/` bug happened.
func (m GgufModel) GgufHTTPPath() string {
	return "/" + GgufDirName + "/" + m.File
}

// EdgeArtefactName is the name this model occupies inside the artefact tree, and
// therefore in manifest.json and in /artifact/<name>. Keeping it under the gguf/
// directory means the same file is reachable both ways: /artifact/ for the
// generic content-addressed fetch path, /gguf/ for the task-shaped one that
// carries the digest in a header.
func (m GgufModel) EdgeArtefactName() string {
	return GgufDirName + "/" + m.File
}

// HubURL is where the same bytes come from when the Space does not have them.
// The hub serves LFS objects at an opaque /resolve/<rev>/<path> path and
// redirects to a CDN, which is why this is a URL to GET rather than a file to
// copy out of a snapshot directory.
func (m GgufModel) HubURL(revision string) string {
	if revision == "" {
		revision = "main"
	}
	return "https://huggingface.co/" + m.Repo + "/resolve/" + revision + "/" + m.File
}
