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
        if !matches!(operation,"add"|"batch"|"search"|"profile"|"reflect"|"outcome"|"check-action"|"propose"|"turn"|"record-put"|"record-search"|"record-forget"|"record-reap-due") {
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
    /// Store a versioned envelope, preserving its payload and provenance.
    pub fn put_record(&self,record:Value)->Result<Value>{
        if !record.is_object(){return Err("Record must be a JSON object".into());}
        self.call("record-put",json!({"record":record}))
    }
    pub fn search_records(&self,query:&str,limit:u32)->Result<Value>{
        self.call("record-search",json!({"query":query,"limit":limit}))
    }
    pub fn forget_record(&self,id:&str,reason:&str)->Result<Value>{
        self.call("record-forget",json!({"id":id,"reason":reason}))
    }
    /// Erase 1..100 due roots and their dependents. Hosted use requires admin scope.
    pub fn reap_due_records(&self,limit:u32)->Result<Value>{
        if !(1..=100).contains(&limit){return Err("Expiration batch must be in 1..100".into());}
        self.call("record-reap-due",json!({"limit":limit}))
    }
    /// Capture and retrieve in one request. Session, query and context stay pinned.
    pub fn turn(&self,session:&str,query:&str,mut options:Value)->Result<Value>{
        let body=options.as_object_mut().ok_or("Turn options must be a JSON object")?;
        body.insert("session_id".into(),json!(session));
        body.insert("query".into(),json!(query));
        self.call("turn",options)
    }
}
#[cfg(test)] mod tests {
    use super::*;
    use std::{io::{BufRead, BufReader, Write}, net::TcpListener, sync::mpsc, thread};

    #[test] fn record_operations_use_scoped_wire_contract() {
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();
        let address=listener.local_addr().unwrap();
        let (tx,rx)=mpsc::channel();
        let operations=["turn","record-put","record-search","record-forget","record-reap-due"];
        let server=thread::spawn(move || {
            for _ in operations {
                let (mut stream,_)=listener.accept().unwrap();
                let mut reader=BufReader::new(stream.try_clone().unwrap());
                let mut first=String::new();reader.read_line(&mut first).unwrap();
                let mut length=0;let mut authorization=String::new();
                loop {
                    let mut line=String::new();reader.read_line(&mut line).unwrap();
                    if line=="\r\n" {break;}
                    if let Some((key,value))=line.split_once(':') {
                        match key.to_ascii_lowercase().as_str() {
                            "content-length"=>length=value.trim().parse().unwrap(),
                            "authorization"=>authorization=value.trim().to_owned(),
                            _=>{}
                        }
                    }
                }
                let mut raw=vec![0;length];reader.read_exact(&mut raw).unwrap();
                tx.send((first,authorization,serde_json::from_slice::<Value>(&raw).unwrap())).unwrap();
                let reply=r#"{"results":[{"deleted":["fact-1"]}],"context":"memory"}"#;
                write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",reply.len(),reply).unwrap();
            }
        });
        let client=MemoryClient::new(&format!("http://{address}"),"secret","alice",vec!["user:u".into()]).unwrap();
        for operation in operations {
            let mut arguments=match operation {
                "turn"=>json!({"session_id":"session","query":"hello"}),
                "record-put"=>json!({"record":{"id":"fact-1","kind":"fact","version":1,"payload":{"text":"hello"}}}),
                "record-search"=>json!({"query":"hello","limit":20}),
                "record-forget"=>json!({"id":"fact-1","reason":"retention"}),
                _=>json!({"limit":16}),
            };
            arguments["context"]=json!(["agent:mallory"]);
            let result=client.call(operation,arguments).unwrap();
            assert_eq!(result["context"],"memory");
            let (first,authorization,request)=rx.recv_timeout(Duration::from_secs(2)).unwrap();
            assert_eq!(first,format!("POST /v1/memory/{operation} HTTP/1.1\r\n"));
            assert_eq!(authorization,"Bearer secret");
            assert_eq!(request["context"],json!(["user:u","agent:alice"]));
        }
        server.join().unwrap();
    }

    #[test] fn record_and_turn_wrappers_preserve_envelope_and_bounds() {
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();
        let address=listener.local_addr().unwrap();
        let (tx,rx)=mpsc::channel();
        let server=thread::spawn(move || {
            for _ in 0..5 {
                let (mut stream,_)=listener.accept().unwrap();
                let mut reader=BufReader::new(stream.try_clone().unwrap());
                let mut first=String::new();reader.read_line(&mut first).unwrap();
                let mut length=0;
                loop {
                    let mut line=String::new();reader.read_line(&mut line).unwrap();
                    if line=="\r\n" {break;}
                    if let Some((key,value))=line.split_once(':') {
                        if key.eq_ignore_ascii_case("content-length") {length=value.trim().parse().unwrap();}
                    }
                }
                let mut raw=vec![0;length];reader.read_exact(&mut raw).unwrap();
                let request=serde_json::from_slice::<Value>(&raw).unwrap();
                let reply=json!({"request":request}).to_string();
                tx.send((first,request)).unwrap();
                write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",reply.len(),reply).unwrap();
            }
        });
        let client=MemoryClient::new(&format!("http://{address}"),"secret","alice",vec!["user:u".into()]).unwrap();
        let record=json!({"id":"fact-1","kind":"fact","version":2,"payload":{"text":"temporary fact","forget_after":"2030-01-01T00:00:00Z","forget_reason":"retention"}});
        assert_eq!(client.put_record(record.clone()).unwrap()["request"]["record"],record);
        let (first,_)=rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(first,"POST /v1/memory/record-put HTTP/1.1\r\n");
        assert_eq!(client.search_records("temporary",3).unwrap()["request"]["limit"],3);
        let (_,request)=rx.recv_timeout(Duration::from_secs(2)).unwrap();assert_eq!(request["query"],"temporary");
        assert_eq!(client.forget_record("fact-1","retention").unwrap()["request"]["reason"],"retention");
        let (_,request)=rx.recv_timeout(Duration::from_secs(2)).unwrap();assert_eq!(request["id"],"fact-1");
        assert_eq!(client.reap_due_records(16).unwrap()["request"]["limit"],16);
        let (first,_)=rx.recv_timeout(Duration::from_secs(2)).unwrap();assert_eq!(first,"POST /v1/memory/record-reap-due HTTP/1.1\r\n");
        let result=client.turn("session","query",json!({"session_id":"spoof","event":"SessionEnd","budget":0,"payload":{"succeeded":true}})).unwrap();
        assert_eq!(result["request"]["session_id"],"session");
        let (_,request)=rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(request["query"],"query");assert_eq!(request["event"],"SessionEnd");assert_eq!(request["budget"],0);
        assert_eq!(request["payload"]["succeeded"],true);assert_eq!(request["context"],json!(["user:u","agent:alice"]));
        for limit in [0,101,u32::MAX] {assert!(client.reap_due_records(limit).is_err());}
        assert!(client.turn("session","query",json!([])).is_err());
        assert!(client.put_record(json!([])).is_err());
        server.join().unwrap();
    }
    #[test] fn insecure_urls_and_non_memory_operations_fail(){
        assert!(MemoryClient::new("http://remote.example","","a",vec![]).is_err());
        assert!(MemoryClient::new("https://user:secret@example.com","","a",vec![]).is_err());
        let c=MemoryClient::new("http://127.0.0.1:9","","a",vec![]).unwrap();
        assert!(c.call("delete",json!({})).is_err());
    }
}
