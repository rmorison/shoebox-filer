-- provenance_files rows of these records go with them (ON DELETE CASCADE).
DELETE FROM receipt.provenance WHERE match_method = 'migrated';
