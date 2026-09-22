// SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
// SPDX-License-Identifier: MPL-2.0

use anyhow::Result;
use serde_json::json;

fn main() -> Result<()> {
    println!("{}", serde_json::to_string(&json!({"hello": "world"}))?);
    Ok(())
}
