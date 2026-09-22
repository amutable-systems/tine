// SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
// SPDX-License-Identifier: MPL-2.0

// A minimal program with a dependency that itself has transitive dependencies,
// so the example exercises the pruned module graph end to end.
package main

import (
	"fmt"

	"rsc.io/quote"
)

func main() {
	fmt.Println(quote.Go())
}
