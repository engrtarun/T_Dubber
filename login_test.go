package main

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The bug these tests exist for: a worker with no terminal asked Telegram for a
// login code anyway, so every upload burned a real OTP on a prompt nobody could
// answer, failed at EOF, and fell back to Telethon. The refusal has to happen
// before anything is sent, which is exactly what loginDecision decides.
func TestLoginDecisionRefusesACodeNobodyCanType(t *testing.T) {
	const session = "/kaggle/working/tgup.session"

	cases := []struct {
		name       string
		authorized bool
		phone      string
		promptable bool
		wantErr    string
	}{
		{
			name:       "authorized session never asks again",
			authorized: true,
			phone:      "+910000000000",
			promptable: false,
		},
		{
			name:       "no session and no phone is the old, honest error",
			authorized: false,
			phone:      "",
			promptable: false,
			wantErr:    "--phone",
		},
		{
			name:       "headless worker with a phone must be refused",
			authorized: false,
			phone:      "+910000000000",
			promptable: false,
			wantErr:    "refusing to request a login code",
		},
		{
			name:       "a console may log in",
			authorized: false,
			phone:      "+910000000000",
			promptable: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := loginDecision(tc.authorized, tc.phone, tc.promptable, session)
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("expected no error, got %v", err)
				}
				return
			}
			if err == nil {
				t.Fatalf("expected an error containing %q, got nil", tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("error %q does not mention %q", err, tc.wantErr)
			}
		})
	}

	// The refusal must name the session, otherwise "it did not log in" and
	// "it logged into the wrong file" look identical in a worker's log.
	err := loginDecision(false, "+910000000000", false, session)
	if err == nil || !strings.Contains(err.Error(), session) {
		t.Errorf("refusal must name the session path, got %v", err)
	}
}

// A refused login must leave the session file untouched: the point of refusing
// is that no half-open auth state is written for the next run to trip over.
func TestSessionCopiesCreatesTheDirectoryButNoSession(t *testing.T) {
	original := sessionFile
	defer func() { sessionFile = original }()

	dir := filepath.Join(t.TempDir(), "working")
	sessionFile = filepath.Join(dir, "tgup.session")

	paths, err := sessionCopies(2)
	if err != nil {
		t.Fatalf("sessionCopies: %v", err)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("session directory was not created: %v", err)
	}
	if _, err := os.Stat(sessionFile); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("a session file appeared out of nowhere: %v", err)
	}
	if len(paths) != 2 {
		t.Errorf("got %d session copies, want 2", len(paths))
	}
}

// --session beats TGUP_SESSION beats the working-directory default, because a
// flag is the most specific statement a caller can make.
func TestApplySessionFlagPrecedence(t *testing.T) {
	original := sessionFile
	defer func() { sessionFile = original }()

	const env = "TGUP_SESSION"
	originalEnv, hadEnv := os.LookupEnv(env)
	defer func() {
		if hadEnv {
			os.Setenv(env, originalEnv)
		} else {
			os.Unsetenv(env)
		}
	}()

	sessionFile = defaultSessionFile
	os.Unsetenv(env)
	applySessionFlag("")
	if sessionFile != defaultSessionFile {
		t.Errorf("empty flag with no env changed the session to %q", sessionFile)
	}

	os.Setenv(env, "/kaggle/working/tgup.session")
	applySessionFlag("")
	if sessionFile != "/kaggle/working/tgup.session" {
		t.Errorf("TGUP_SESSION not honoured, got %q", sessionFile)
	}

	applySessionFlag("  /tmp/explicit.session  ")
	if sessionFile != "/tmp/explicit.session" {
		t.Errorf("--session must win over the env var, got %q", sessionFile)
	}
}
