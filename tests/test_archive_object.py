from io import BytesIO
import pytest
from compass.archive_object import run
from compass.store import Store


class Objects:
    def __init__(self,corrupt=False):self.data={};self.corrupt=corrupt
    def put_object(self,Bucket,Key,Body,**kwargs):self.data[Key]=Body.read() if hasattr(Body,'read') else Body
    def get_object(self,Bucket,Key):
        data=self.data[Key]
        return {'Body':BytesIO(data[:-1]+b'x' if self.corrupt else data)}


def test_object_readback_restore_and_receipt(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'source.sqlite'));db.initialize()
    with db.tx() as c:db.append(c,'quote','fixture','ABC',123,{'bid':1,'ask':2,'ts':123})
    objects=Objects();result=run(db,objects,'private')
    assert result['rows']==1 and result['durable_copy_verified'] and result['isolated_restore_verified']
    assert result['receipt_readback_verified'] and result['database_rows_deleted']==0
    assert not result['cross_chunk_completeness_verified']
    assert len(objects.data)==2
    with pytest.raises(ValueError,match='checksum'):run(db,Objects(True),'private')
    db.engine.dispose()
