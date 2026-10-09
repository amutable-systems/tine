// SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
// SPDX-License-Identifier: MPL-2.0

package main

import "testing"

func TestMessage(t *testing.T) {
	if message != "no dependencies here\n" {
		t.Fatalf("unexpected message: %q", message)
	}
}
