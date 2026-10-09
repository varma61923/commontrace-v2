//! Scoped memory API shared by the local binary and hosted HTTP gateway.
use reqwest::{blocking::Client as Http, redirect::Policy, Url};
use serde_json::{Value, json};
use std::{error::Error, io::Read, time::Duration};
pub type Result<T> = std::result::Result<T, Box<dyn Error + Send + Sync>>;
pub struct MemoryClient { base: String, token: String, context: Vec<String>, http: Http }
impl MemoryClient {
    pub fn new(base: &str, token: &str, agent: &str, scopes: Vec<String>) -> Result<Self> {
        let url=Url::parse(base)?;
        let local=matches!(url.host_str(),Some("localhost"|"127.0.0.1"|"[::1]"|"::1"));
        if !url.username().is_empty() || url.password().is_some() || url.query().is_some() || url.fragment().is_some()
            || !(url.scheme()=="https" || (url.scheme()=="http" && local)) {
            return Err("Use HTTPS or loopback URL without credentials".into());
        }
        let mut context=scopes;
        if !agent.is_empty() {context.push(format!("agent:{agent}"));}
        let http=Http::builder().timeout(Duration::from_secs(30)).redirect(Policy::none()).build()?;
        Ok(Self {base:base.trim_end_matches('/').into(),token:token.into(),context,http})
    }
    pub fn call(&self,operation:&str,mut data:Value)->Result<Value>{
        if !matches!(operation,"add"|"batch"|"search"|"profile"|"reflect"|"outcome"|"check-action"|"propose") {
            return Err("Unknown memory operation".into());
        }
        let map=data.as_object_mut().ok_or("Request must be a JSON object")?;
        map.insert("context".into(),json!(self.context));
        let mut request=self.http.post(format!("{}/v1/memory/{operation}",self.base)).json(&data);
        if !self.token.is_empty(){request=request.bearer_auth(&self.token);}
        let response=request.send()?;
        if !response.status().is_success(){return Err(format!("Gateway HTTP {}",response.status()).into());}
        let mut raw=Vec::new();response.take(8*1024*1024+1).read_to_end(&mut raw)?;
        if raw.len()>8*1024*1024{return Err("Response too large".into());}
        let value:Value=serde_json::from_slice(&raw)?;
        if !value.is_object(){return Err("Response must be a JSON object".into());}
        Ok(value)
    }
    pub fn add(&self,text:&str)->Result<Value>{self.call("add",json!({"text":text,"local":true}))}
    pub fn search(&self,query:&str)->Result<Value>{self.call("search",json!({"query":query}))}
    pub fn profile(&self,query:&str)->Result<Value>{self.call("profile",json!({"query":query}))}
    pub fn reflect(&self,query:&str,occasion:&str,budget:u32)->Result<Value>{
        self.call("reflect",json!({"query":query,"occasion_id":occasion,"budget":budget}))
    }
    pub fn outcome(&self,occasion:&str,succeeded:bool)->Result<Value>{
        self.call("outcome",json!({"occasion_id":occasion,"succeeded":succeeded}))
    }
}
#[cfg(test)] mod tests {
    use super::*;
    #[test] fn insecure_urls_and_non_memory_operations_fail(){
        assert!(MemoryClient::new("http://remote.example","","a",vec![]).is_err());
        assert!(MemoryClient::new("https://user:secret@example.com","","a",vec![]).is_err());
        let c=MemoryClient::new("http://127.0.0.1:9","","a",vec![]).unwrap();
        assert!(c.call("delete",json!({})).is_err());
    }
}
