// SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
// SPDX-License-Identifier: MPL-2.0

// A module with no dependencies at all, which therefore carries no go.sum.
// Embedding checks that the source view exposes regular files to the compiler.
package main

import (
	_ "embed"
	"fmt"
)

//go:embed message.txt
var message string

func main() {
	fmt.Print(message)
}
