use anyhow::Result;
use serde_json::json;

fn main() -> Result<()> {
    println!("{}", serde_json::to_string(&json!({"hello": "world"}))?);
    Ok(())
}
