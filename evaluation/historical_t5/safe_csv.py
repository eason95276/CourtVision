"""Project CSV fields without materializing skipped target outcome values.

The stream necessarily traverses bytes, but excluded field contents are not
tokenized into values, returned, logged or persisted. Gating may use gameId,
which is the first column in the admitted NBA historical source files.
"""
import csv

def projected_rows(path,fields,*,allowed_game_ids=None):
    with open(path,encoding='utf-8-sig',newline='')as file:
        header=next(csv.reader([file.readline()]));wanted={i:k for i,k in enumerate(header)if k in fields}
        if allowed_game_ids is not None and header[0]not in {'gameId','game_id'}:raise ValueError('game_id_must_precede_outcome_columns')
        index=0;quoted=False;pending_quote=False;value=[];row={};eligible=allowed_game_ids is None
        def commit():
            nonlocal index,value,row,eligible
            if index==0 and allowed_game_ids is not None:
                ident=''.join(value);eligible=ident.split(':')[-1].zfill(10)in allowed_game_ids
                if eligible and 0 in wanted:row[wanted[0]]=ident
            elif index in wanted and eligible:row[wanted[index]]=''.join(value).replace('\r\n','\n')
            value=[];index+=1
        for line in file:
            for char in line:
                keep=(index in wanted and eligible)or(index==0 and allowed_game_ids is not None)
                if quoted:
                    if pending_quote:
                        if char=='"':
                            if keep:value.append('"')
                            pending_quote=False;continue
                        quoted=False;pending_quote=False
                    elif char=='"':pending_quote=True;continue
                    else:
                        if keep:value.append(char)
                        continue
                if char=='"'and not value:quoted=True;continue
                if char==',':commit();continue
                if char in '\r\n':
                    if char=='\r':continue
                    commit()
                    if eligible and row:yield row
                    index=0;value=[];row={};eligible=allowed_game_ids is None;continue
                if keep:value.append(char)
        if value or row:
            commit()
            if eligible and row:yield row
