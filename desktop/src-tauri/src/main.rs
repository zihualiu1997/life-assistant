#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use base64::Engine;
use keyring::Entry;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager,
};

#[derive(Deserialize, Serialize)]
struct Connection {
    endpoint: String,
    token: String,
}
fn entry() -> Result<Entry, String> {
    Entry::new("LifeAssistant", "server-device").map_err(|_| "无法访问 Windows 凭据库。".into())
}
fn read_connection() -> Result<Connection, String> {
    let s = entry()?.get_password().map_err(|_| "请先配对服务器。")?;
    serde_json::from_str(&s).map_err(|_| "连接配置损坏，请重新配对。".into())
}
fn url(value: &str) -> Result<String, String> {
    let u = reqwest::Url::parse(value.trim()).map_err(|_| "服务器地址无效。")?;
    if u.scheme() != "https"
        || u.host_str().is_none()
        || !u.username().is_empty()
        || u.password().is_some()
        || u.query().is_some()
        || u.fragment().is_some()
        || u.path() != "/"
    {
        return Err("请填写 HTTPS 服务器根地址。".into());
    }
    Ok(u.as_str().trim_end_matches('/').into())
}
fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(140))
        .build()
        .map_err(|_| "无法初始化连接。".into())
}
async fn decode(mut response: reqwest::Response) -> Result<Value, String> {
    if !response.status().is_success() {
        return Err(match response.status().as_u16() {
            401 | 403 => "设备未授权或凭据无效，请重新配对。",
            409 => "记录发生冲突，请先查询历史。",
            429 => "请求过于频繁，请稍后重试。",
            _ => "服务器未完成操作。写入或聊天结果可能待确认，请保留原文并查看历史。",
        }
        .into());
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| "连接中断，结果待确认。")?
    {
        if bytes.len() + chunk.len() > 2_000_000 {
            return Err("响应过大。".into());
        }
        bytes.extend_from_slice(&chunk)
    }
    serde_json::from_slice(&bytes).map_err(|_| "服务器响应无效。".into())
}
#[tauri::command]
fn connection() -> Value {
    match read_connection() {
        Ok(c) => json!({"endpoint":c.endpoint,"configured":true}),
        Err(_) => json!({"configured":false}),
    }
}
#[tauri::command]
async fn pair(endpoint: String, code: String) -> Result<(), String> {
    let endpoint = url(&endpoint)?;
    let result = decode(
        client()?
            .post(format!("{endpoint}/v1/pair"))
            .json(&json!({"code":code,"name":"Windows 桌面宠物"}))
            .send()
            .await
            .map_err(|_| "配对请求失败。配对码可能已使用，请在服务器撤销未使用设备后重新生成。")?,
    )
    .await?;
    let token = result["token"]
        .as_str()
        .ok_or("未返回设备凭据。")?
        .to_owned();
    let c = Connection { endpoint, token };
    entry()?
        .set_password(&serde_json::to_string(&c).map_err(|_| "保存失败。")?)
        .map_err(|_| "无法保存 Windows 凭据。".into())
}
#[tauri::command]
fn forget() -> Result<(), String> {
    match entry()?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(_) => Err("无法清除本机连接。".into()),
    }
}
#[tauri::command]
async fn request(action: String, data: Value) -> Result<Value, String> {
    let c = read_connection()?;
    let base = url(&c.endpoint)?;
    let http = client()?;
    let req = match action.as_str() {
        "health" => http.get(format!("{base}/v1/health")),
        "history" => http.get(format!("{base}/v1/history")),
        "chat" => http.post(format!("{base}/v1/chat")).json(&data),
        "record" => http.post(format!("{base}/v1/journal")).json(&data),
        "journal" | "plan" | "morning" | "evening" => {
            http.get(format!("{base}/v1/query")).query(&[
                ("kind", action.as_str()),
                ("date", data["date"].as_str().ok_or("请选择日期。")?),
            ])
        }
        _ => return Err("不支持的操作。".into()),
    };
    decode(
        req.bearer_auth(c.token)
            .send()
            .await
            .map_err(|_| "连接中断，操作结果待确认。请保留原文；写入重试会沿用原编号。")?,
    )
    .await
}
#[tauri::command]
fn pet() -> String {
    base64::engine::general_purpose::STANDARD.encode(include_bytes!("../../assets/pet.webp"))
}
fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            connection, pair, forget, request, pet
        ])
        .setup(|app| {
            let show = MenuItem::with_id(app, "show", "显示生活助手", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;
            let mut tray =
                TrayIconBuilder::new()
                    .menu(&menu)
                    .on_menu_event(|app, event| match event.id.as_ref() {
                        "show" => {
                            if let Some(w) = app.get_webview_window("main") {
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        }
                        "quit" => app.exit(0),
                        _ => {}
                    });
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone())
            }
            tray.build(app)?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("desktop startup failed")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reject_credential_and_non_https_urls() {
        for s in [
            "http://example.com",
            "https://user:pass@example.com",
            "https://example.com/path",
            "https://example.com/?token=x",
        ] {
            assert!(url(s).is_err())
        }
        assert_eq!(
            url("https://life.example.com/").unwrap(),
            "https://life.example.com"
        );
    }
}
