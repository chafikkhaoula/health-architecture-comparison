package main

import (
	"errors"
	"net/http"
	"testing"
)

func TestClassifyContractErrors(t *testing.T) {
	tests := []struct {
		message string
		code    string
		status  int
	}{
		{
			message: "HAC_RECORD_ALREADY_EXISTS: duplicate",
			code:    "RECORD_ALREADY_EXISTS",
			status:  http.StatusConflict,
		},
		{
			message: "HAC_RECORD_NOT_FOUND: missing",
			code:    "RECORD_NOT_FOUND",
			status:  http.StatusNotFound,
		},
		{
			message: "HAC_RULE_ID_CONFLICT: reused",
			code:    "RULE_ID_CONFLICT",
			status:  http.StatusConflict,
		},
		{
			message: "HAC_INVALID_ARGUMENT: invalid actorId",
			code:    "INVALID_ARGUMENT",
			status:  http.StatusBadRequest,
		},
	}
	for _, test := range tests {
		code, status := classifyError(errors.New(test.message))
		if code != test.code || status != test.status {
			t.Fatalf(
				"classifyError(%q) = (%q, %d), want (%q, %d)",
				test.message,
				code,
				status,
				test.code,
				test.status,
			)
		}
	}
}

func TestNormalizedResult(t *testing.T) {
	if got := string(normalizedResult(nil)); got != "null" {
		t.Fatalf("empty result = %s, want null", got)
	}
	if got := string(normalizedResult([]byte(`{"ok":true}`))); got !=
		`{"ok":true}` {
		t.Fatalf("JSON result changed: %s", got)
	}
	if got := string(normalizedResult([]byte("plain"))); got !=
		`"plain"` {
		t.Fatalf("plain result = %s, want quoted JSON string", got)
	}
}
