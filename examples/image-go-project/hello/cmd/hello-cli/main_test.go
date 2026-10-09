// SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
// SPDX-License-Identifier: MPL-2.0

package main

import (
	"strings"
	"testing"

	"rsc.io/quote"
)

func TestQuote(t *testing.T) {
	if got := quote.Go(); !strings.Contains(got, "communicating") {
		t.Fatalf("unexpected quote: %q", got)
	}
}
