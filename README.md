# An-Explainable-Human-in-the-Loop-Framework-for-Con-flict-of-Interest-Screening-
An Explainable Human-in-the-Loop Framework for Con-flict-of-Interest Screening Using Logical Rules and Online Re-lationship Verification
Data set 

https://dataverse.tdl.org/dataset.xhtml?persistentId=doi:10.18738/T8/GBSTTH


First Program
fetch_pubmed_authors.py  
Location in manuscript: Section 5.1, Algorithm 1
Retrieves author names, publication titles, journals, and dates from PubMed. As explained in Section 5.1 of your manuscript, the original disclosure dataset does not contain all the author information required for relationship analysis. Therefore, the program enriches each publication record using its PubMed identifier (PMID).
This program provides the data-enrichment component of the framework. It connects COI disclosure records with their corresponding authors, thereby creating the information needed for subsequent author–organization relationship analysis.
Second Program
author_org_relation_checker.py
Location in manuscript: Section 5.2, Algorithm 2
Searches PubMed, Crossref, and optionally public web-search results for evidence of author–organization relationships. The second Python program implements the primary automated relationship-verification component of the proposed framework. It investigates whether publicly available information supports an association between a specific author and an organization mentioned in the COI disclosure. This is directly related to the computational verification of the predicate: relation(A,B). where (A) represents the author or agent and (B) represents the organization or relevant entity. 
PubMed verification: It constructs a query combining author information with the organization name in the affiliation field. The program retrieves up to 10 candidate publications and examines their author names and affiliation metadata. A match is recorded when the author-matching and organization-matching conditions are satisfied.
Crossref verification: It searches Crossref using the author and organization names and examines up to five returned publications. The program checks whether the matched author has affiliation metadata containing the organization.
principal contribution is to transform candidate relationships extracted from textual COI disclosures into evidence-supported assessments based on external information sources. It also maintains an explanation of the assigned verdict and stores available evidence links, helping make the verification procedure traceable.

Third Program
COI_PubMed_Crossref_Separate_Validator.py
Location in manuscript: Section 6, Evaluation.
Audits candidate relationships separately against PubMed and Crossref, recording evidence, incomplete searches, and retrieval failures. This program was designed to evaluate the operational performance of the author–organization verification procedure.
The program examines the ability of PubMed and Crossref to independently return supporting affiliation evidence for candidate pairs. It provides the computational basis for the source-specific evaluation described in Section 6.
The program conducts two separate source-specific searches.
PubMed: The program searches for publications associated with the target author, using a default time window of five years before and after the original publication year. It retrieves up to 180 candidate records and checks author-specific affiliations.
Crossref: The program uses both an affiliation-focused query and a broader author query. It retrieves up to 100 records per query and evaluates whether the target author is associated with the specified organization.
Both sources are assessed separately, and the program records the evidence identified by each.

