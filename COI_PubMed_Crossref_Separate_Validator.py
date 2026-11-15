#!/usr/bin/env python3
"""Separate PubMed/Crossref author-specific affiliation audits of COIPonD candidate pairs.

Ethics and inference: PubMed and Crossref index publisher/article affiliation metadata.
'Not observed' is NOT evidence of an absent relationship, and a COI disclosure is
NOT a gold label for employment, consulting or other financial arrangements.

Usage:
  pip install requests
  python COI_PubMed_Crossref_Separate_Validator.py --input processed_pairs.csv \
    --email YOUR_EMAIL --outdir results --max-rows 400

Input columns (case-insensitive aliases): pmid, author, organization,
relationship_type (optional), system_verdict (optional), evidence_pmids/doi (optional),
reference_label (optional: SUPPORTED/UNSUPPORTED), reference_basis (optional).
Output: pubmed_evidence.csv, crossref_evidence.csv, paired_audit.csv, summary.json.

Precision/recall/accuracy are computed ONLY if explicit independent reference
labels are supplied (and the reference_basis is not disclosure or tested metadata).
Otherwise the program reports source-specific corroboration without invented metrics.
"""
import argparse,csv,json,re,sys,time,unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from collections import Counter
from datetime import datetime,timezone
from xml.etree import ElementTree as ET
from urllib.parse import quote
import requests

EUTIL='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
CR='https://api.crossref.org/works'
S=requests.Session()
CACHE={}

FIELDS={
 'pmid':['pmid','pubmed_id','pubmedid'],
 'author':['author','author_name'],
 'organization':['organization','organization_name','org'],
 'relationship_type':['relationship_type','relationship_type_inferred','relation_type'],
 'system_verdict':['system_verdict','result','verdict','verification_result','relationship_verdict'],
 'doi':['doi'],
 'evidence_pmids':['evidence_pmids','supporting_pmids','pubmed_pmids','matched_pmids'],
 'evidence_dois':['evidence_dois','supporting_dois'],
 'reference_label':['reference_label','gold_label','independent_reference_label'],
 'reference_basis':['reference_basis','gold_source','reference_source'],
}
STOP=set('inc incorporated ltd limited llc corp corporation company co the research laboratories university of and for center centre'.split())

def norm(text):
    text=unicodedata.normalize('NFKD',str(text or '')).casefold()
    text=''.join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r'[^a-z0-9]+',' ',text).strip()

def orgmatch(org,affiliation):
    a=norm(affiliation); o=norm(org)
    if not a or len(o)<4:return False
    if re.search(r'(?<!\w)'+re.escape(o)+r'(?!\w)',a):return True
    ot=[x for x in o.split() if x not in STOP and len(x)>=3]
    if not ot:return False
    if len(ot)==1:return bool(len(ot[0])>=5 and re.search(r'\b'+re.escape(ot[0])+r'\b',a))
    return all(bool(re.search(r'\b'+re.escape(t)+r'\b',a)) for t in ot) and len(ot)>=2

def author_match(query,rec):
    """High-precision surname + initials check; namesake risk remains."""
    def split(s):
        s=norm(s).split()
        return (s[-1], ''.join(w[0] for w in s[:-1])) if len(s)>=2 else ('','')
    qa,qi=split(query); ra,ri=split(rec)
    if not qa or not ra or qa!=ra or not qi or not ri:return False
    return qi==ri or (len(qi)==1 and qi==ri[0]) or (len(ri)==1 and ri==qi[0])

def canonical_doi(s):
    x=str(s or '').strip().lower()
    x=re.sub(r'^(?:doi:|https?://(?:dx\.)?doi\.org/)','',x)
    return x.rstrip(' .,/;)')

def get(url,params=None,kind='json'):
    params=dict(params or {});key=(url,tuple(sorted((k,str(v)) for k,v in params.items())),kind)
    if key in CACHE:return CACHE[key]
    for trial in range(3):
        try:
            rsp=S.get(url,params=params,timeout=18)
            if rsp.status_code==404:CACHE[key]=None;return None
            if rsp.status_code in (429,500,502,503,504):time.sleep(1+2**trial);continue
            rsp.raise_for_status()
            val=rsp.json() if kind=='json' else rsp.text
            CACHE[key]=val;time.sleep(0.4 if 'ncbi.nlm.nih' in url else 0.15)
            return val
        except (requests.RequestException,ValueError) as exc:
            if trial==2:print(f'Network/API failure for {url}: {exc}',file=sys.stderr);return None
            time.sleep(1+trial)
    return None

def nlm_params(email):return {'tool':'coi_separate_source_validation','email':email}

def fetch_pubmed_xml(ids,email):
    ids=[x for x in dict.fromkeys(str(i) for i in ids) if x]
    if not ids:return []
    params={**nlm_params(email),'db':'pubmed','id':','.join(ids),'retmode':'xml'}
    body=get(EUTIL+'efetch.fcgi',params,'text')
    if body is None:return None
    try:return list(ET.fromstring(body).findall('./PubmedArticle'))
    except ET.ParseError:return None

def parse_pubmed_article(x):
    pmid=(x.findtext('./MedlineCitation/PMID') or '').strip()
    doi=''
    for el in x.findall('./PubmedData/ArticleIdList/ArticleId'):
        if el.attrib.get('IdType')=='doi':doi=canonical_doi(el.text);break
    for idel in x.findall('./MedlineCitation/Article/ELocationID'):
        if not doi and idel.attrib.get('EIdType')=='doi':doi=canonical_doi(idel.text)
    authors=[]
    for au in x.findall('./MedlineCitation/Article/AuthorList/Author'):
        given=au.findtext('ForeName') or au.findtext('Initials') or ''
        last=au.findtext('LastName') or ''
        affs=[' '.join((aff.itertext())) for aff in au.findall('./AffiliationInfo/Affiliation')]
        authors.append({'name':(given+' '+last).strip(),'affiliations':affs})
    year=x.findtext('./MedlineCitation/Article/Journal/JournalIssue/PubDate/Year')
    if not year:
        medline=x.findtext('./MedlineCitation/Article/Journal/JournalIssue/PubDate/MedlineDate') or ''
        ym=re.search(r'\b(?:19|20)\d{2}\b',medline);year=ym.group(0) if ym else ''
    return {'pmid':pmid,'doi':doi,'year':year,'authors':authors}

def source_result(state,evidence=[],checked=0,count=0,detail=''):
    return {'status':state,'evidence':evidence,'checked':checked,'candidate_count':count,'detail':detail}

def get_index(pmid,email):
    hits=fetch_pubmed_xml([pmid],email)
    if hits is None:return None,'PUBMED_FETCH_ERROR'
    if not hits:return None,'INDEX_PMID_UNAVAILABLE'
    return parse_pubmed_article(hits[0]),''

def exclude_set(row,index):
    badp={str(row.get('pmid') or '').strip()}
    badd={canonical_doi(index.get('doi') or row.get('doi') or '')}
    for n in re.findall(r'\b\d{6,9}\b',str(row.get('evidence_pmids') or '')):badp.add(n)
    for n in re.split(r'[;,|\s]+',str(row.get('evidence_dois') or '')):
        if canonical_doi(n):badd.add(canonical_doi(n))
    return badp,{d for d in badd if d}

def verify_pubmed(row,index,email,limit,year_window):
    name=row['author'];org=row['organization'];tokens=norm(name).split()
    if len(tokens)<2:return source_result('SKIPPED',detail='Non-individual author or name missing')
    if not index:return source_result('API_ERROR',detail='Index article metadata unavailable')
    y=str(index.get('year',''))
    last,initials=tokens[-1],''.join(s[0] for s in tokens[:-1])
    term=f'"{last} {initials}"[Author]'
    if y.isdigit():term+=f' AND {max(1940,int(y)-year_window)}:{int(y)+year_window}[dp]'
    search=get(EUTIL+'esearch.fcgi',{**nlm_params(email),'db':'pubmed','term':term,'retmode':'json','retmax':limit})
    if not search:return source_result('API_ERROR',detail='PubMed ESearch failed')
    result=search.get('esearchresult',{}); ids=result.get('idlist',[])
    count=int(result.get('count',0)); badp,badd=exclude_set(row,index)
    ids=[p for p in ids if p not in badp]
    evidence=[];checked=0
    for offset in range(0,len(ids),50):
        items=fetch_pubmed_xml(ids[offset:offset+50],email)
        if items is None:return source_result('API_ERROR',evidence,checked,count,'EFetch failed after search')
        for item in items:
            p=parse_pubmed_article(item)
            if p['pmid'] in badp or (p['doi'] and p['doi'] in badd):continue
            checked+=1
            for au in p['authors']:
                if author_match(name,au['name']):
                    for aff in au['affiliations']:
                        if orgmatch(org,aff):
                            evidence.append({'id':p['pmid'],'doi':p['doi'],'url':'https://pubmed.ncbi.nlm.nih.gov/'+p['pmid']+'/','author':au['name'],'affiliation':aff})
                            break
    if evidence:return source_result('SUPPORTED',evidence,checked,count)
    state='SEARCH_TRUNCATED' if count>limit else 'NOT_OBSERVED'
    return source_result(state,[],checked,count,'Not observed is not proof of absence')

def crossref_author_name(a):
    return ' '.join([str(a.get('given') or ''),str(a.get('family') or '')]).strip()

def crossref_affs(a):
    result=[]
    for af in a.get('affiliation',[]) or []:
        if isinstance(af,str):result.append(af)
        elif isinstance(af,dict):
            if af.get('name'):result.append(str(af['name']))
            if af.get('institution'):
                arr=af['institution'];arr=arr if isinstance(arr,list) else [arr]
                for inst in arr:
                    if isinstance(inst,dict) and inst.get('name'):result.append(inst['name'])
    return result

def verify_crossref(row,index,email,limit,year_window):
    name=row['author'];org=row['organization']
    if not index:return source_result('API_ERROR',detail='Cannot exclude index DOI')
    y=str(index.get('year','')); filters={'query.author':name,'rows':min(limit,100),'mailto':email}
    if y.isdigit(): filters['filter']=f'from-pub-date:{int(y)-year_window}-01-01,until-pub-date:{int(y)+year_window}-12-31'
    # Execute affiliation-focused and broad author query separately; do not assume
    # that matching an organization anywhere on the article proves an author link.
    candidates={};network_fail=0;total_count=0
    for focused in (True,False):
        params=dict(filters)
        if focused:params['query.affiliation']=org
        obj=get(CR,params)
        if not obj:
            network_fail+=1;continue
        msg=obj.get('message',{});total_count=max(total_count,int(msg.get('total-results',0)))
        for paper in msg.get('items',[]):
            doi=canonical_doi(paper.get('DOI',''))
            if doi:candidates[doi]=paper
    if network_fail==2:return source_result('API_ERROR',detail='Crossref search unavailable')
    badp,badd=exclude_set(row,index);evidence=[];checked=0
    for doi,paper in candidates.items():
        if doi in badd:continue
        checked+=1
        for au in paper.get('author',[]):
            if not author_match(name,crossref_author_name(au)):continue
            for affiliation in crossref_affs(au):
                if orgmatch(org,affiliation):
                    evidence.append({'id':doi,'doi':doi,'url':'https://doi.org/'+doi,
                                     'author':crossref_author_name(au),'affiliation':affiliation})
                    break
    if evidence:return source_result('SUPPORTED',evidence,checked,total_count)
    state='SEARCH_TRUNCATED' if total_count>limit else 'NOT_OBSERVED'
    return source_result(state,[],checked,total_count,'Crossref affiliation deposits may be incomplete')

def normal_record(row):
    keymap={re.sub(r'[^a-z0-9]','',str(k).lower()):v for k,v in row.items()}
    out={}
    for target,aliases in FIELDS.items():
        out[target]=''
        for alias in aliases:
            value=keymap.get(re.sub(r'[^a-z0-9]','',alias.lower()))
            if value is not None:
                out[target]=str(value).strip();break
    return out

def csv_read(path):
    if path.suffix.lower()=='.csv':
        with open(path,encoding='utf-8-sig',newline='') as f:return [normal_record(r) for r in csv.DictReader(f)]
    if path.suffix.lower() in ('.xlsx','.xls'):
        try:import pandas as pd
        except ImportError:raise RuntimeError('For Excel inputs install pandas and openpyxl')
        return [normal_record(r) for r in pd.read_excel(path,dtype=str).fillna('').to_dict('records')]
    raise ValueError('Please provide .csv or .xlsx input')

def csv_write(path,records,headers):
    with open(path,'w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=headers,extrasaction='ignore');writer.writeheader();writer.writerows(records)

def score(rows,field):
    data=[]
    for r in rows:
        label=norm(r.get('reference_label',''))
        basis=norm(r.get('reference_basis',''))
        if label not in ('supported','unsupported'):continue
        if not basis or any(x in basis for x in ('disclosure','pubmed','crossref','algorithm 2','algorithm2','same paper')):continue
        predicted=str(r.get(field,'')).upper()
        if field=='system_verdict':
            if predicted in ('FOUND','CONFIRMED'):pred=True
            elif predicted in ('INCONCLUSIVE','REJECTED'):pred=False
            else:continue
        else:
            if predicted=='SUPPORTED':pred=True
            elif predicted=='NOT_OBSERVED':pred=False
            else:continue
        data.append((label=='supported',pred))
    if not data:return {'n':0,'precision':None,'recall':None,'accuracy':None,'tp':None,'fp':None,'fn':None,'tn':None,
       'note':'No eligible independent binary reference labels; original disclosures and tested sources do not count.'}
    tp=sum(y and p for y,p in data);fn=sum(y and not p for y,p in data)
    fp=sum(not y and p for y,p in data);tn=sum(not y and not p for y,p in data)
    return {'n':len(data),'tp':tp,'fp':fp,'fn':fn,'tn':tn,
        'precision':tp/(tp+fp) if tp+fp else None,
        'recall':tp/(tp+fn) if tp+fn else None,
        'accuracy':(tp+tn)/len(data),
        'note':'Only with independently established reference labels. Coverage/selection limitations remain.'}

def main():
    pa=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    pa.add_argument('--input',required=True);pa.add_argument('--email',required=True)
    pa.add_argument('--outdir',default='coi_separate_source_results');pa.add_argument('--max-rows',type=int,default=400)
    pa.add_argument('--pubmed-candidates',type=int,default=180);pa.add_argument('--crossref-candidates',type=int,default=100)
    pa.add_argument('--year-window',type=int,default=5);pa.add_argument('--fail-fast',action='store_true')
    args=pa.parse_args();path=Path(args.input)
    if not path.is_file():pa.error('Processed COIPonD pair-level file not found: '+str(path))
    if '@' not in args.email:pa.error('NCBI E-utilities requests require a contact email')
    S.headers['User-Agent']='COIPonD-source-specific-validation/1.0 (mailto:'+args.email+')'
    data=csv_read(path)
    if args.max_rows>0:data=data[:args.max_rows]
    data=[r for r in data if r['pmid'] and r['author'] and r['organization']]
    if not data:pa.error('No valid candidate rows (required: pmid, author, organization)')
    outdir=Path(args.outdir);outdir.mkdir(parents=True,exist_ok=True)
    pubrows=[];crossrows=[];paired=[];indexed={};failed=0
    for i,row in enumerate(data,1):
        pmid=row['pmid'];pmid=pmid.removesuffix('.0')
        if not pmid.isdigit():continue
        if pmid not in indexed:indexed[pmid]=get_index(pmid,args.email)
        index,err=indexed[pmid]
        pub=verify_pubmed(row,index,args.email,args.pubmed_candidates,args.year_window)
        cro=verify_crossref(row,index,args.email,args.crossref_candidates,args.year_window)
        if pub['status']=='API_ERROR' or cro['status']=='API_ERROR':failed+=1
        base={**row,'pmid':pmid}
        pr={**base,'source':'PubMed','source_status':pub['status'],'evidence_count':len(pub['evidence']),
             'candidate_count':pub['candidate_count'],'checked':pub['checked'],
             'evidence_json':json.dumps(pub['evidence'],ensure_ascii=False),'detail':pub['detail'] or err}
        cr={**base,'source':'Crossref','source_status':cro['status'],'evidence_count':len(cro['evidence']),
             'candidate_count':cro['candidate_count'],'checked':cro['checked'],
             'evidence_json':json.dumps(cro['evidence'],ensure_ascii=False),'detail':cro['detail'] or err}
        pubrows.append(pr);crossrows.append(cr)
        pdoi={x['doi'] for x in pub['evidence'] if x['doi']};cdoi={x['doi'] for x in cro['evidence'] if x['doi']}
        paired.append({**base,'pubmed_status':pub['status'],'crossref_status':cro['status'],
           'pubmed_evidence_count':len(pub['evidence']),'crossref_evidence_count':len(cro['evidence']),
           'shared_underlying_dois':';'.join(sorted(pdoi&cdoi)),
           'distinct_evidence_dois':len(pdoi|cdoi),
           'both_supported':pub['status']=='SUPPORTED' and cro['status']=='SUPPORTED'})
        if args.fail_fast and failed:break
        if i%20==0:print(f'Processed {i}/{len(data)} pairs',file=sys.stderr)
    fields=list(FIELDS)+['source','source_status','evidence_count','candidate_count','checked','evidence_json','detail']
    csv_write(outdir/'pubmed_evidence.csv',pubrows,fields)
    csv_write(outdir/'crossref_evidence.csv',crossrows,fields)
    csv_write(outdir/'paired_audit.csv',paired,list(FIELDS)+['pubmed_status','crossref_status',
       'pubmed_evidence_count','crossref_evidence_count','shared_underlying_dois','distinct_evidence_dois','both_supported'])
    result={'timestamp_utc':datetime.now(timezone.utc).isoformat(),'input':str(path),
       'total_records_considered':len(paired),'pubmed':dict(Counter(p['pubmed_status'] for p in paired)),
       'crossref':dict(Counter(p['crossref_status'] for p in paired)),
       'supported_in_both':sum(bool(p['both_supported']) for p in paired),
       'metrics':{k:score(paired,k) for k in ('system_verdict','pubmed_status','crossref_status')},
       'warnings':['Affiliation does not prove consulting/funding/COI or impropriety.',
         'Absence of a match is not evidence of absence.',
         'Two source records about the same underlying publication are not independent observations.',
         'Excluding the original PMID does not exclude prior Algorithm 2 supporting papers unless evidence IDs were supplied.',
         'Source-specific reports are verification coverage; metrics need independently supported binary labels.']}
    (outdir/'summary.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(result,indent=2,ensure_ascii=False))

if __name__=='__main__':main()
