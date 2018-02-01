#pylint:disable=missing-docstring,unused-argument,no-self-use
import os, sys, copy, json, itertools, gc
from alembic.Abc import OArchive, IArchive, OStringProperty
from alembic.AbcGeom import OXform, OPolyMesh, IXform, IPolyMesh
from Qt.QtCore import QAbstractItemModel, QModelIndex, Qt, QSortFilterProxyModel
from utils import getNextName


CONTEXT = os.path.basename(sys.executable)
if CONTEXT == "maya.exe":
	from mayaInterface import DCC
elif CONTEXT == "XSI.exe":
	from xsiInterface import DCC
else:
	from dummyInterface import DCC


# Abstract Items
class Falloff(object):
	def __init__(self, name, simplex, *data):
		self.name = name
		self.children = []
		self._buildIdx = None
		self.splitType = data[0]
		self.expanded = False
		self.simplex = simplex
		self.simplex.falloffs.append(self)

		if self.splitType == "planar":
			self.axis = data[1]
			self.maxVal = data[2]
			self.maxHandle = data[3]
			self.minHandle = data[4]
			self.minVal = data[5]
			self.mapName = None
		elif self.splitType == "map":
			self.axis = None
			self.maxVal = None
			self.maxHandle = None
			self.minHandle = None
			self.minVal = None
			self.mapName = data[1]

	@classmethod
	def createPlanar(cls, name, simplex, axis, maxVal, maxHandle, minHandle, minVal):
		return cls(name, simplex, 'planar', axis, maxVal, maxHandle, minHandle, minVal)

	@classmethod
	def createMap(cls, name, simplex, mapName):
		return cls(name, simplex, 'map', mapName)

	def buildDefinition(self, simpDict):
		if self._buildIdx is None:
			if self.splitType == "planar":
				line = ["planar", self.axis, self.maxVal, self.maxHandle, self.minHandle, self.minVal]
			else:
				line = ["map", self.mapName]
			simpDict.setdefault("falloffs", []).append([self.name] + line)
			self._buildIdx = len(simpDict["falloffs"]) - 1
		return self._buildIdx

	def clearBuildIndex(self):
		self._buildIdx = None

	def duplicate(self, newName):
		""" duplicate a falloff with a new name """
		nf = copy.copy(self)
		nf.name = newName
		nf.children = []
		nf.clearBuildIndex()
		self.simplex.falloffs.append(nf)
		self.simplex.DCC.duplicateFalloff(self, nf, newName)
		return nf

	def delete(self):
		""" delete a falloff """
		fIdx = self.simplex.falloffs.index(self)
		for child in self.children:
			child.falloff = None
		self.simplex.falloffs.pop(fIdx)

		self.simplex.DCC.deleteFalloff(self)

	def setPlanarData(self, splitType, axis, minVal, minHandle, maxHandle, maxVal):
		""" set the type/data for a falloff """
		self.splitType = "planar"
		self.axis = axis
		self.minVal = minVal
		self.minHandle = minHandle
		self.maxHandle = maxHandle
		self.maxVal = maxVal
		self.mapName = None
		self._updateDCC()

	def setMapData(self, mapName):
		""" set the type/data for a falloff """
		self.splitType = "map"
		self.axis = None
		self.minVal = None
		self.minHandle = None
		self.maxHandle = None
		self.maxVal = None
		self.mapName = mapName
		self._updateDCC()

	def _updateDCC(self):
		self.simplex.DCC.setFalloffData(self, self.splitType, self.axis, self.minVal,
						  self.minHandle, self.maxHandle, self.maxVal, self.mapName)


class Shape(object):
	classDepth = 7
	def __init__(self, name, simplex):
		self._thing = None
		self._thingRepr = None
		self._name = name
		self._buildIdx = None
		simplex.shapes.append(self)
		self.isRest = False
		self.expanded = False
		self.simplex = simplex
		# maybe Build thing on creation?

	@classmethod
	def createShape(cls, name, simplex, slider=None):
		''' Convenience method for creating a new shape
		This will create all required parent objects to have a new shape
		'''
		if simplex.restShape is None:
			raise RuntimeError("Simplex system is missing rest shape")

		if slider is None:
			# Implicitly creates a shape
			slider = Slider.createSlider(name, simplex)
			for p in slider.prog.pairs:
				if p.shape.name == name:
					return p.shape
			raise RuntimeError("Problem creating shape with proper name")
		else:
			if slider.simplex != simplex:
				raise RuntimeError("Slider does not belong to the provided Simplex")
			tVal = slider.prog.guessNextTVal()
			pp = slider.prog.createShape(name, tVal)
			return pp.shape

	@property
	def sliderModel(self):
		try:
			return self.simplex.sliderModel
		except AttributeError:
			pass
		return None

	@property
	def comboModel(self):
		try:
			return self.simplex.comboModel
		except AttributeError:
			pass
		return None

	@property
	def name(self):
		return self._name

	@name.setter
	def name(self, value): ### Data Changed (Slider, Combo)
		self._name = value
		self.simplex.DCC.renameShape(self, value)

	@property
	def thing(self):
		# if this is a deepcopied object, then self._thing will
		# be None.	Rebuild the thing connection by its representation
		if self._thing is None and self._thingRepr:
			self._thing = DCC.loadPersistentShape(self._thingRepr)
		return self._thing

	@thing.setter
	def thing(self, value):
		self._thing = value
		self._thingRepr = DCC.getPersistentShape(value)

	def buildDefinition(self, simpDict):
		if self._buildIdx is None:
			simpDict.setdefault("shapes", []).append(self.name)
			self._buildIdx = len(simpDict["shapes"]) - 1
		return self._buildIdx

	def clearBuildIndex(self):
		self._buildIdx = None

	def __deepcopy__(self, memo):
		# DO NOT make a copy of the DCC thing
		# as it may or may not be a persistent object
		cls = self.__class__
		result = cls.__new__(cls)
		memo[id(self)] = result
		for k, v in self.__dict__.iteritems():
			if k == "_thing":
				setattr(result, k, None)
			else:
				setattr(result, k, copy.deepcopy(v, memo))
		return result

	def zeroShape(self):
		""" Set the shape to be completely zeroed """
		self.simplex.DCC.zeroShape(self)

	@staticmethod
	def zeroShapes(shapes):
		for shape in shapes:
			if not shape.isRest:
				shape.zeroShape()

	def connectShape(self, mesh=None, live=False, delete=False):
		""" Force a shape to match a mesh
			The "connect shape" button is:
				mesh=None, delete=True
			The "match shape" button is:
				mesh=someMesh, delete=False
			There is a possibility of a "make live" button:
				live=True, delete=False
		"""
		self.simplex.DCC.connectShape(self, mesh, live, delete)

	@staticmethod
	def connectShapes(shapes, meshes, live=False, delete=False):
		for shape, mesh in zip(shapes, meshes):
			shape.connectShape(mesh, live, delete)


class ProgPair(object):
	classDepth = 6
	def __init__(self, shape, value):
		self.shape = shape
		self._value = value
		self.prog = None
		self.minValue = -1.0
		self.maxValue = 1.0
		self.expanded = False

	@property
	def name(self):
		return self.shape.name

	def buildDefinition(self, simpDict):
		idx = self.shape.buildDefinition(simpDict)
		return idx, self.value

	def __lt__(self, other):
		return self.value < other.value

	@property
	def value(self):
		return self._value

	@value.setter
	def value(self, val): ### Data Changed(Combo, Slider)
		self._value = val


class Progression(object):
	classDepth = 5
	def __init__(self, name, simplex, pairs=None, interp="spline", falloffs=None):
		self.simplex = simplex
		if self.comboModel:
			par = self.parent('Combo')
			parIdx = self.comboModel.indexFromItem(par)
			rowCount = self.comboModel.rowCount(parIdx)
			self.comboModel.beginInsertRows(parIdx, rowCount, rowCount)

		self.name = name
		self.interp = interp
		self.falloffs = falloffs or []
		self.controller = None

		if pairs is None:
			self.pairs = [ProgPair(self.simplex.restShape, 0.0)]
		else:
			self.pairs = pairs

		for pair in self.pairs:
			pair.prog = self

		for falloff in self.falloffs:
			falloff.children.append(self)
		self._buildIdx = None
		self.expanded = False

		if self.comboModel:
			self.comboModel.endInsertRows()

	@property
	def sliderModel(self):
		try:
			return self.simplex.sliderModel
		except AttributeError:
			pass
		return None

	@property
	def comboModel(self):
		try:
			return self.simplex.comboModel
		except AttributeError:
			pass
		return None

	def getShapeIndex(self, shape):
		for i, p in enumerate(self.pairs):
			if p.shape == shape:
				return i
		raise ValueError("Provided shape:{0} is not in the list".format(shape.name))

	def getShapes(self):
		return [i.shape for i in self.pairs]

	def buildDefinition(self, simpDict):
		if self._buildIdx is None:
			idxPairs = [pair.buildDefinition(simpDict) for pair in self.pairs]
			idxPairs.sort(key=lambda x: x[1])
			idxs, values = zip(*idxPairs)
			foIdxs = [f.buildDefinition(simpDict) for f in self.falloffs]
			x = [self.name, idxs, values, self.interp, foIdxs]
			simpDict.setdefault("progressions", []).append(x)
			self._buildIdx = len(simpDict["progressions"]) - 1
		return self._buildIdx

	def clearBuildIndex(self):
		self._buildIdx = None
		for pair in self.pairs:
			pair.shape.clearBuildIndex()
		for fo in self.falloffs:
			fo.clearBuildIndex()

	def moveShapeToProgression(self, shapePair): ### Moves Rows (Slider, Combo)
		""" Remove the shapePair from its current progression
		and set it in a new progression """
		oldProg = shapePair.prog
		oldProg.pairs.remove(shapePair)
		self.pairs.append(shapePair)
		shapePair.prog = self

	def setShapesValues(self, values): ### Data Changed (Slider, Combo)
		""" Set the shape's value in it's progression """
		for pp, val in zip(self.pairs, values):
			pp.value = val
		if isinstance(self.parent, Slider):
			self.simplex.DCC.updateSlidersRange([self.parent])

	def addFalloff(self, falloff):
		""" Add a falloff to a slider's falloff list """
		self.falloffs.append(falloff)
		falloff.children.append(self)
		self.simplex.DCC.addProgFalloff(self, falloff)

	def removeFalloff(self, falloff):
		""" Remove a falloff from a slider's falloff list """
		self.falloffs.remove(falloff)
		falloff.children.remove(self)
		self.simplex.DCC.removeProgFalloff(self, falloff)

	def createShape(self, shapeName=None, tVal=None):
		""" create a shape and add it to a progression """
		if self.sliderModel:
			selfIdx = self.sliderModel.indexFromItem(self)
			rowInsert = len(self.pairs)
			self.sliderModel.beginInsertRows(selfIdx, rowInsert, rowInsert)

		if self.comboModel:
			selfIdx = self.comboModel.indexFromItem(self)
			rowInsert = len(self.pairs)
			self.comboModel.beginInsertRows(selfIdx, rowInsert, rowInsert)

		if tVal is None:
			tVal = self.guessNextTVal()

		if shapeName is None:
			if abs(tVal) == 1.0:
				shapeName = self.controller.name
			else:
				shapeName = "{0}_{1}".format(self.controller.name, int(abs(tVal)*100))
			currentNames = [i.name for i in self.simplex.shapes]
			shapeName = getNextName(shapeName, currentNames)

		shape = Shape(shapeName, self.simplex)
		pp = ProgPair(shape, tVal)
		pp.prog = self
		self.pairs.append(pp)

		self.simplex.DCC.createShape(shapeName, pp)
		if isinstance(self.parent, Slider):
			self.simplex.DCC.updateSlidersRange([self.parent])

		if self.sliderModel:
			self.sliderModel.endInsertRows()
		if self.comboModel:
			self.comboModel.endInsertRows()

		return pp

	def guessNextTVal(self):
		''' Given the current progression values, make an
		educated guess what's next.
		'''
		# The question remains if negative or
		# intermediate values are more important
		# I think intermediate
		vals = [i.value for i in self.pairs]
		mnv = min(vals)
		mxv = max(vals)
		if mnv == 0.0 and mxv == 1.0:
			for c in [0.5, 0.25, 0.75, -1.0]:
				if c not in vals:
					return c
		if mnv == -1.0 and mxv == 1.0:
			for c in [0.5, -0.5, 0.25, -0.25, 0.75, -0.75]:
				if c not in vals:
					return c
		return 1.0

	def deleteShape(self, shape):
		if self.sliderModel:
			selfIdx = self.sliderModel.indexFromItem(self)
			rowRem = shape.getRow('Slider')
			self.sliderModel.beginRemoveRows(selfIdx, rowRem, rowRem)

		if self.comboModel:
			selfIdx = self.comboModel.indexFromItem(self)
			rowRem = shape.getRow('Combo')
			self.comboModel.beginRemoveRows(selfIdx, rowRem, rowRem)

		ridx = None
		for i, pp in enumerate(self.pairs):
			if pp.shape == shape:
				ridx = i
		if ridx is None:
			raise RuntimeError("Shape does not exist to remove")
		self.pairs.pop(ridx)
		if not shape.isRest:
			self.simplex.shapes.remove(shape)
			self.simplex.DCC.deleteShape(shape)

		if self.sliderModel:
			self.sliderModel.endRemoveRows()

		if self.comboModel:
			self.comboModel.endRemoveRows()

	def delete(self):
		if self.comboModel:
			parIdx = self.comboModel.indexFromItem(self.parent('Combo'))
			rowRem = self.getRow('Combo')
			self.comboModel.beginRemoveRows(parIdx, rowRem, rowRem)

		for pp in self.pairs[:]:
			if pp.shape.isRest:
				continue
			self.simplex.shapes.remove(pp.shape)
			self.simplex.DCC.deleteShape(pp.shape)

		if self.comboModel:
			self.comboModel.endRemoveRows()


class Slider(object):
	classDepth = 4
	def __init__(self, name, simplex, prog, group):
		if group.groupType != type(self):
			raise ValueError("Cannot add this slider to a combo group")

		self.simplex = simplex
		self.group = group
		self._name = name
		self._thing = None
		self._thingRepr = None
		self.prog = prog
		self.split = False
		self.prog.controller = self
		self._buildIdx = None
		self._value = 0.0
		self.minValue = -1.0
		self.maxValue = 1.0
		self.expanded = False
		self.multiplier = 1

		if self.sliderModel:
			parIdx = self.sliderModel.indexFromItem(self.parent('Slider'))
			rowCount = self.sliderModel.rowCount(parIdx)
			self.sliderModel.beginInsertRows(parIdx, rowCount, rowCount)

		self.group.items.append(self)
		self.simplex.sliders.append(self)

		if self.sliderModel:
			self.sliderModel.endInsertRows()

	@classmethod
	def createSlider(cls, name, simplex, group=None, shape=None, tVal=1.0, multiplier=1):
		"""
		Create a new slider with a name in a group.
		Possibly create a single default shape for this slider
		"""
		if simplex.restShape is None:
			raise RuntimeError("Simplex system is missing rest shape")

		if group is None:
			if simplex.sliderGroups:
				group = simplex.sliderGroups[0]
			else:
				group = Group('{0}_GROUP'.format(name), simplex, Slider)

		currentNames = [s.name for s in simplex.sliders]
		name = getNextName(name, currentNames)

		prog = Progression(name, simplex)
		if shape is None:
			prog.createShape(name, tVal)
		else:
			prog.pairs.append(ProgPair(shape, tVal))

		sli = cls(name, simplex, prog, group)
		simplex.DCC.createSlider(name, sli, multiplier=multiplier)
		sli.multiplier = multiplier
		return sli

	@property
	def sliderModel(self):
		try:
			return self.simplex.sliderModel
		except AttributeError:
			pass
		return None

	@property
	def name(self):
		return self._name

	@name.setter
	def name(self, value): ### Data Changed (Slider, Combo)
		""" Set the name of a slider """
		self.name = value
		self.prog.name = value
		self.simplex.DCC.renameSlider(self, value, self.multiplier)
		# TODO Also rename the combos

	@property
	def thing(self):
		# if this is a deepcopied object, then self._thing will
		# be None.	Rebuild the thing connection by its representation
		if self._thing is None and self._thingRepr:
			self._thing = DCC.loadPersistentSlider(self._thingRepr)
		return self._thing

	@thing.setter
	def thing(self, value):
		self._thing = value
		self._thingRepr = DCC.getPersistentSlider(value)

	@property
	def value(self):
		return self._value

	@value.setter
	def value(self, val): ### Data Changed (Slider)
		self._value = val

	def buildDefinition(self, simpDict):
		if self._buildIdx is None:
			gIdx = self.group.buildDefinition(simpDict)
			pIdx = self.prog.buildDefinition(simpDict)
			simpDict.setdefault("sliders", []).append([self.name, pIdx, gIdx])
			self._buildIdx = len(simpDict["sliders"]) - 1
		return self._buildIdx

	def clearBuildIndex(self):
		self._buildIdx = None
		self.prog.clearBuildIndex()
		self.group.clearBuildIndex()

	def __deepcopy__(self, memo):
		# DO NOT make a copy of the DCC thing
		# as it may or may not be a persistent object
		cls = self.__class__
		result = cls.__new__(cls)
		memo[id(self)] = result
		for k, v in self.__dict__.iteritems():
			if k == "_thing":
				setattr(result, k, None)
			else:
				setattr(result, k, copy.deepcopy(v, memo))
		return result

	def setRange(self, multiplier):
		self.simplex.DCC.setSliderRange(self, multiplier)

	def delete(self):
		""" Delete a slider, any shapes it contains, and all downstream combos """
		self.simplex.deleteDownstreamCombos(self)
		if self.sliderModel:
			parIdx = self.sliderModel.indexFromItem(self.parent('Slider'))
			row = self.getRow('Slider')
			self.sliderModel.beginRemoveRows(parIdx, row, row)

		g = self.group
		g.items.remove(self)
		self.group = None
		self.simplex.sliders.remove(self)
		self.prog.delete()
		self.simplex.DCC.deleteSlider(self)

		if self.sliderModel:
			self.sliderModel.endRemoveRows()

	def setInterpolation(self, interp):
		""" Set the interpolation of a slider """
		self.prog.interp = interp

	def extractProgressive(self, live=True, offset=10.0, separation=5.0):
		pos, neg = [], []
		for pp in sorted(self.prog.pairs):
			if pp.value < 0.0:
				neg.append((pp.value, pp.shape, offset))
				offset += separation
			elif pp.value > 0.0:
				pos.append((pp.value, pp.shape, offset))
				offset += separation
			#skip the rest value at == 0.0
		neg = reversed(neg)

		for prog in [pos, neg]:
			xtVal, shape, shift = prog[-1]
			ext, deltaShape = self.simplex.DCC.extractWithDeltaShape(shape, live, shift)
			for value, shape, shift in prog[:-1]:
				ext = self.simplex.DCC.extractWithDeltaConnection(shape, deltaShape, value/xtVal, live, shift)

	def extractShape(self, shape, live=True, offset=10.0):
		return self.simplex.DCC.extractShape(shape, live, offset)

	def connectShape(self, shape, mesh=None, live=False, delete=False):
		self.simplex.DCC.connectShape(shape, mesh, live, delete)


class ComboPair(object):
	classDepth = 3
	def __init__(self, slider, value):
		self.slider = slider
		self._value = float(value)
		self.minValue = -1.0
		self.maxValue = 1.0
		self.combo = None
		self.expanded = False

	@property
	def name(self):
		return self.slider.name

	@property
	def value(self):
		return self._value

	@value.setter
	def value(self, val): ### Data Changed (Combo)
		self._value = val

	def buildDefinition(self, simpDict):
		sIdx = self.slider.buildDefinition(simpDict)
		return sIdx, self.value


class Combo(object):
	classDepth = 2
	def __init__(self, name, simplex, pairs, prog, group):
		self.simplex = simplex
		if self.comboModel:
			parIdx = self.comboModel.indexFromItem(self.parent('Combo'))
			rowCount = self.comboModel.rowCount(parIdx)
			self.comboModel.beginInsertRows(parIdx, rowCount, rowCount)

		if group.groupType != type(self):
			raise ValueError("Cannot add this slider to a combo group")
		self._name = name
		self.pairs = pairs
		self.prog = prog
		self.group = group
		self.prog.controller = self
		self.group.items.append(self)
		self._buildIdx = None
		for p in self.pairs:
			p.combo = self
		self.expanded = False
		self.simplex.combos.append(self)

		if self.comboModel:
			self.comboModel.endInsertRows()

	@property
	def comboModel(self):
		try:
			return self.simplex.comboModel
		except AttributeError:
			pass
		return None

	@classmethod
	def createCombo(cls, name, simplex, sliders, values, group=None, shape=None, tVal=1.0):
		""" Create a combo of sliders at values """
		if simplex.restShape is None:
			raise RuntimeError("Simplex system is missing rest shape")

		if group is None:
			gname = "DEPTH_{0}".format(len(sliders))
			matches = [i for i in simplex.comboGroups if i.name == gname]
			if matches:
				group = matches[0]
			else:
				group = Group(gname, simplex, Combo)

		cPairs = [ComboPair(slider, value) for slider, value in zip(sliders, values)]
		prog = Progression(name, simplex)
		if shape:
			prog.pairs.append(ProgPair(shape, tVal))

		cmb = Combo(name, simplex, cPairs, prog, group)

		if shape is None:
			pp = prog.createShape(name, tVal)
			simplex.DCC.zeroShape(pp.shape)

		return cmb

	@property
	def name(self):
		return self._name

	@name.setter
	def name(self, value): ### Data Changed (Combo)
		""" Set the name of a combo """
		self._name = value
		self.prog.name = value
		self.simplex.DCC.renameCombo(self, value)

	def getSliderIndex(self, slider):
		for i, p in enumerate(self.pairs):
			if p.slider == slider:
				return i
		raise ValueError("Provided slider:{0} is not in the list".format(slider.name))

	def isFloating(self):
		for pair in self.pairs:
			if abs(pair.value) != 1.0:
				return True
		return False

	def getSliders(self):
		return [i.slider for i in self.pairs]

	def buildDefinition(self, simpDict):
		if self._buildIdx is None:
			gIdx = self.group.buildDefinition(simpDict)
			pIdx = self.prog.buildDefinition(simpDict)
			idxPairs = [p.buildDefinition(simpDict) for p in self.pairs]
			x = [self.name, pIdx, idxPairs, gIdx]
			simpDict.setdefault("combos", []).append(x)
			self._buildIdx = len(simpDict["combos"]) - 1
		return self._buildIdx

	def clearBuildIndex(self):
		self._buildIdx = None
		self.prog.clearBuildIndex()
		self.group.clearBuildIndex()

	def extractProgressive(self, live=True, offset=10.0, separation=5.0):
		raise RuntimeError('Currently just copied from Sliders, Not actually real')
		pos, neg = [], []
		for pp in sorted(self.prog.pairs):
			if pp.value < 0.0:
				neg.append((pp.value, pp.shape, offset))
				offset += separation
			elif pp.value > 0.0:
				pos.append((pp.value, pp.shape, offset))
				offset += separation
			#skip the rest value at == 0.0
		neg = reversed(neg)

		for prog in [pos, neg]:
			xtVal, shape, shift = prog[-1]
			ext, deltaShape = self.simplex.DCC.extractWithDeltaShape(shape, live, shift)
			for value, shape, shift in prog[:-1]:
				ext = self.simplex.DCC.extractWithDeltaConnection(shape, deltaShape, value/xtVal, live, shift)

	def extractShape(self, shape, live=True, offset=10.0):
		""" Extract a shape from a combo progression """
		return self.simplex.DCC.extractComboShape(self, shape, live, offset)

	def connectComboShape(self, shape, mesh=None, live=False, delete=False):
		""" Connect a shape into a combo progression"""
		self.simplex.DCC.connectComboShape(self, shape, mesh, live, delete)

	def delete(self):
		""" Delete a combo and any shapes it contains """
		if self.comboModel:
			parIdx = self.comboModel.indexFromItem(self.parent('Combo'))
			row = self.getRow('Combo')
			self.comboModel.beginRemoveRows(parIdx, row, row)

		g = self.group
		if self not in g.combos:
			return # Can happen when deleting multiple groups
		g.items.remove(self)
		self.group = None
		self.simplex.combos.remove(self)
		pairs = self.prog.pairs[:] # gotta make a copy
		for pair in pairs:
			pair.delete()

		if self.comboModel:
			self.comboModel.endRemoveRows()

	def setInterpolation(self, interp): ### Data Changed (Combo)
		""" Set the interpolation of a combo """
		self.prog.interp = interp

	def setComboValue(self, slider, value): ### Data Changed (Combo)
		""" Set the Slider/value pairs for a combo """
		idx = self.getSliderIndex(slider)
		self.pairs[idx].value = value

	def appendComboValue(self, slider, value):
		""" Append a Slider/value pair for a combo """
		if self.comboModel:
			selfIdx = self.comboModel.indexFromItem(self)
			rowInsert = len(self.pairs)
			self.comboModel.beginInsertRows(selfIdx, rowInsert, rowInsert)

		cp = ComboPair(slider, value)
		self.pairs.append(cp)
		cp.combo = self

		if self.comboModel:
			self.comboModel.endInsertRows()

	def deleteComboPair(self, comboPair): ### Removes Rows (Combo)
		""" delete a Slider/value pair for a combo """
		if self.comboModel:
			rowRem = comboPair.getRow('Combo')
			selfIdx = self.comboModel.indexFromItem(self)
			self.comboModel.beginRemoveRows(selfIdx, rowRem, rowRem)

		# We specifically don't move the combo to the proper depth group
		# That way the user can make multiple changes to the combo without
		# it popping all over in the heirarchy
		self.pairs.remove(comboPair)
		comboPair.combo = None

		if self.comboModel:
			self.comboModel.endRemoveRows()


class Group(object):
	classDepth = 1
	def __init__(self, name, simplex, groupType):
		self._name = name
		self.items = []
		self._buildIdx = None
		self.expanded = False
		self.groupType = groupType
		self.simplex = simplex

		if self.groupType == Slider and self.sliderModel:
			par = self.parent('Slider')
			parIdx = self.sliderModel.indexFromItem(par)
			rowIns = par.rowCount('Slider')
			self.sliderModel.beginInsertRows(parIdx, rowIns, rowIns)
		if self.groupType == Combo and self.comboModel:
			par = self.parent('Combo')
			parIdx = self.comboModel.indexFromItem(par)
			rowIns = par.rowCount('Combo')
			self.comboModel.beginInsertRows(parIdx, rowIns, rowIns)

		if self.groupType is Slider:
			self.simplex.sliderGroups.append(self)
		elif self.groupType is Combo:
			self.simplex.comboGroups.append(self)

		if self.sliderModel:
			self.sliderModel.endInsertRows()
		if self.comboModel:
			self.comboModel.endInsertRows()

	@property
	def name(self):
		return self._name

	@name.setter
	def name(self, value):
		self._name = value
		if self.groupType == Slider and self.sliderModel:
			idx = self.sliderModel.indexFromItem(self)
			self.sliderModel.dataChanged.emit(idx, idx)
		if self.groupType == Combo and self.comboModel:
			idx = self.comboModel.indexFromItem(self)
			self.comboModel.dataChanged.emit(idx, idx)

	@classmethod
	def createGroup(cls, name, simplex, things=None, groupType=None):
		''' Convenience method for creating a group '''

		thingType = None
		if things is not None:
			tps = list(set([type(i) for i in things]))
			if len(tps) != 1:
				raise RuntimeError("Cannot set both sliders and combos of a group")
			thingType = tps[0]

		if groupType is None:
			if thingType is None:
				raise RuntimeError("Must pass either a list of things, or a groupType")
			groupType = thingType
		else:
			if thingType is not None:
				if groupType is not thingType:
					raise RuntimeError("Group Type must match the type of all things")

		g = cls(name, simplex, groupType)
		if things is not None:
			g.take(things)
		return g

	@property
	def sliderModel(self):
		try:
			return self.simplex.sliderModel
		except AttributeError:
			pass
		return None

	@property
	def comboModel(self):
		try:
			return self.simplex.comboModel
		except AttributeError:
			pass
		return None

	def buildDefinition(self, simpDict):
		if self._buildIdx is None:
			simpDict.setdefault("groups", []).append(self.name)
			self._buildIdx = len(simpDict["groups"]) - 1
		return self._buildIdx

	def clearBuildIndex(self):
		self._buildIdx = None

	def delete(self):
		""" Delete a group. Any objects in this group will
		be deleted """

		if self.sliderModel:
			par = self.sliderModel.indexFromItem(self.parent('Slider'))
			row = self.getRow('Slider')
			self.sliderModel.beginRemoveRows(par, row, row)
		if self.comboModel:
			par = self.comboModel.indexFromItem(self.parent('Combo'))
			row = self.getRow('Combo')
			self.comboModel.beginRemoveRows(par, row, row)

		if self.groupType is Slider:
			if len(self.simplex.sliderGroups) == 1:
				return
			self.simplex.sliderGroups.remove(self)
		elif self.groupType is Combo:
			if len(self.simplex.comboGroups) == 1:
				return
			self.simplex.comboGroups.remove(self)

		# Gotta iterate over copies of the lists
		# as .delete removes the items from the list
		for item in self.items[:]:
			item.delete()

		if self.sliderModel:
			self.sliderModel.endRemoveRows()
		if self.comboModel:
			self.comboModel.endRemoveRows()

	def take(self, things): ### Moves Rows (Slider)
		if not all([isinstance(i, self.groupType) for i in things]):
			raise ValueError("All items in this group must be of type: {}".format(self.groupType))

		# do it this way instead of using set() to keep order
		for thing in things:
			if thing not in self.items:
				self.items.append(thing)
				thing.group = self


class Simplex(object):
	classDepth = 0
	'''
	The main Top-level abstract object that controls an entire simplex setup
	'''
	# CONSTRUCTORS
	def __init__(self, name="", sliderModel=None, comboModel=None):
		self._name = name # The name of the system
		self.sliders = [] # List of contained sliders
		self.combos = [] # List of contained combos
		self.sliderGroups = [] # List of groups containing sliders
		self.comboGroups = [] # List of groups containing combos
		self.falloffs = [] # List of contained falloff objects
		self.shapes = [] # List of contained shape objects
		self.restShape = None # Name of the rest shape
		self.clusterName = "Shape" # Name of the cluster (XSI use only)
		self.expanded = False # Am I expanded? (Keep around for consistent interface)
		self.comboExpanded = False # Am I expanded in the combo tree
		self.sliderExpanded = False # Am I expanded in the slider tree
		self.DCC = DCC(self) # Interface to the DCC
		self.sliderModel = sliderModel # Link to the Qt Item Slider model
		self.comboModel = comboModel # Link to the Qt Item Combo model

	def _initValues(self):
		self._name = "" # The name of the system
		self.sliders = [] # List of contained sliders
		self.combos = [] # List of contained combos
		self.sliderGroups = [] # List of groups containing sliders
		self.comboGroups = [] # List of groups containing combos
		self.falloffs = [] # List of contained falloff objects
		self.shapes = [] # List of contained shape objects
		self.restShape = None # Name of the rest shape
		self.clusterName = "Shape" # Name of the cluster (XSI use only)
		self.expanded = False # Am I expanded? (Keep around for consistent interface)
		self.comboExpanded = False # Am I expanded in the combo tree
		self.sliderExpanded = False # Am I expanded in the slider tree

	@property
	def name(self):
		''' Property getter for the simplex name '''
		return self._name

	@name.setter
	def name(self, value):
		""" rename a system and all objects in it """
		self._name = value
		self.DCC.renameSystem(value) #??? probably needs work
		if self.restShape is not None:
			self.restShape.name = self._buildRestName()

		if self.sliderModel:
			idx = self.sliderModel.indexFromItem(self, 0, 'Slider')
			self.sliderModel.dataChanged.emit(idx, idx)

		if self.comboModel:
			idx = self.comboModel.indexFromItem(self, 0, 'Combo')
			self.comboModel.dataChanged.emit(idx, idx)

	@property
	def progs(self):
		out = []
		for slider in self.sliders:
			out.append(slider.prog)
		for combo in self.combos:
			out.append(combo.prog)
		return out

	@classmethod
	def buildBlank(cls, thing, name):
		''' Create a new system on a given mesh, ready to go '''
		self = cls(name)
		self.DCC.loadNodes(self, thing, create=True)
		self.buildRest()
		return self

	@classmethod
	def buildFromJson(cls, thing, jsonPath):
		""" Create a new system based on a path to a json file """
		with open(jsonPath, 'r') as f:
			js = json.load(f)
		return cls.buildFromDict(thing, js)

	@classmethod
	def buildFromDict(cls, thing, simpDict):
		""" Create a new system based on a parsed simplex dictionary """
		self = cls.buildBlank(thing, simpDict['systemName'])
		self.loadFromDict(simpDict, thing, True)

	@classmethod
	def buildFromAbc(cls, abcPath):
		""" Build a system from a simplex abc file """
		iarch, abcMesh, js = cls.getAbcDataFromPath(abcPath)
		try:
			rest = DCC.buildRestAbc(abcMesh, js)
			self = cls.buildBlank(rest, js['systemName'])
			self.loadFromAbc(rest, abcMesh, js)
		finally:
			del iarch, abcMesh
			gc.collect()
		return self

	# LOADERS
	def buildRest(self):
		""" create/find the system's rest shape"""
		if self.restShape is None:
			self.restShape = Shape(self._buildRestName(), self)
			self.restShape.isRest = True

		if not self.restShape.thing:
			pp = ProgPair(self.restShape, 1.0) # just to pass to createShape
			self.DCC.createShape(self.restShape.name, pp)
		return self.restShape

	def loadFromDict(self, simpDict, thing, create):
		''' Load the data from a dictionary onto the current system
		Build any DCC objects that are missing if create=True '''
		self.loadDefinition(simpDict)
		self.DCC.loadNodes(self, thing, create=create)
		self.DCC.loadConnections(self, create=create)

	def loadFromAbc(self, thing, abcMesh, simpDict):
		''' Load a system and shapes from a parsed smpx file
		Uses the return values from `self.getAbcDataFromPath`
		'''
		self.DCC.loadAbc(abcMesh, simpDict)
		self.loadFromDict(simpDict, thing, True)

	# HELPER
	@staticmethod
	def getAbcDataFromPath(abcPath):
		''' Read and return the relevant data from a simplex alembic '''
		iarch = IArchive(str(abcPath)) # because alembic hates unicode
		try:
			top = iarch.getTop()
			par = top.children[0]
			par = IXform(top, par.getName())
			abcMesh = par.children[0]
			abcMesh = IPolyMesh(par, abcMesh.getName())

			systemSchema = par.getSchema()
			props = systemSchema.getUserProperties()
			prop = props.getProperty("simplex")
			jsString = prop.getValue()
			js = json.loads(jsString)

		except Exception: #pylint: disable=broad-except
			del iarch
		return iarch, abcMesh, js

	def comboExists(self, sliders, values):
		''' Check if a combo exists with these specific sliders and values
		Because combo names aren't necessarily always in the same order
		'''
		checkSet = set([(s.name, v) for s, v in zip(sliders, values)])
		for cmb in self.combos:
			cmbSet = set([(p.slider.name, p.value) for p in cmb.pairs])
			if checkSet == cmbSet:
				return cmb
		return None

	# DESTRUCTOR
	def deleteSystem(self):
		''' Delete an existing system from file '''
		# Store the models as temp so the model doesn't go crazy with the signals
		sliderModel, self.sliderModel = self.sliderModel, None
		comboModel, self.comboModel = self.comboModel, None
		if sliderModel:
			sliderModel.beginResetModel()
		if comboModel:
			comboModel.beginResetModel()

		self.DCC.deleteSystem()
		self._initValues()
		self.DCC = DCC(self)

		if sliderModel:
			sliderModel.endResetModel()
		if comboModel:
			comboModel.endResetModel()

		# Reset the models
		self.sliderModel = sliderModel
		self.comboModel = comboModel

	def deleteDownstreamCombos(self, slider):
		todel = []
		for c in self.combos:
			for pair in c.pairs:
				if pair.slider == slider:
					todel.append(c)
		for c in todel:
			c.delete()

	# USER METHODS
	def getFloatingShapes(self):
		''' Find combos that don't have fully extreme activations '''
		floaters = [c for c in self.combos if c.isFloating()]
		floatShapes = []
		for f in floaters:
			floatShapes.extend(f.prog.getShapes())
		return floatShapes

	def buildDefinition(self):
		''' Create a simplex dictionary
		Loop through all the objects managed by this simplex system, and
		build a dictionary that defines it
		'''
		things = [self.sliders, self.combos, self.sliderGroups, self.comboGroups, self.falloffs]
		for thing in things:
			for i in thing:
				i.clearBuildIndex()

		# Make sure that all parts are defined first
		d = {}
		d["encodingVersion"] = 1
		d["systemName"] = self.name
		d["clusterName"] = self.clusterName
		d["falloffs"] = []
		d["combos"] = []
		d["shapes"] = []
		d["sliders"] = []
		d["groups"] = []
		d["progressions"] = []

		# rest shape should *ALWAYS* be index 0
		for shape in self.shapes:
			shape.buildDefinition(d)

		for group in self.sliderGroups:
			group.buildDefinition(d)

		for group in self.comboGroups:
			group.buildDefinition(d)

		for falloff in self.falloffs:
			falloff.buildDefinition(d)

		for slider in self.sliders:
			slider.buildDefinition(d)

		for combo in self.combos:
			combo.buildDefinition(d)

		return d

	def loadDefinition(self, simpDict):
		''' Build the structure of objects in this system
		based on a provided dictionary'''

		self.name = simpDict["systemName"]
		self.clusterName = simpDict["clusterName"] # for XSI
		self.falloffs = [Falloff(f[0], self, *f[1]) for f in simpDict["falloffs"]]
		groupNames = simpDict["groups"]
		shapes = [Shape(s, self) for s in simpDict["shapes"]]
		self.restShape = shapes[0]
		self.restShape.isRest = True

		progs = []
		for p in simpDict["progressions"]:
			progShapes = [shapes[i] for i in p[1]]
			progFalloffs = [self.falloffs[i] for i in p[4]]
			progPairs = map(ProgPair, progShapes, p[2])
			progs.append(Progression(p[0], self, progPairs, p[3], progFalloffs))

		self.sliders = []
		self.sliderGroups = []
		createdSlidergroups = {}
		for s in simpDict["sliders"]:
			sliderProg = progs[s[1]]

			gn = groupNames[s[2]]
			if gn in createdSlidergroups:
				sliderGroup = createdSlidergroups[gn]
			else:
				sliderGroup = Group(gn, self, Slider)
				createdSlidergroups[gn] = sliderGroup

			sli = Slider(s[0], self, sliderProg, sliderGroup)

		self.combos = []
		self.comboGroups = []
		createdComboGroups = {}
		for c in simpDict["combos"]:
			prog = progs[c[1]]
			sliderIdxs, sliderVals = zip(*c[2])
			sliders = [self.sliders[i] for i in sliderIdxs]
			pairs = map(ComboPair, sliders, sliderVals)
			if len(c) >= 4:
				gn = groupNames[c[3]]
			else:
				gn = "DEPTH_0"

			if gn in createdComboGroups:
				comboGroup = createdComboGroups[gn]
			else:
				comboGroup = Group(groupNames[c[3]], self, Combo)
				createdComboGroups[gn] = comboGroup

			cmb = Combo(c[0], self, pairs, prog, comboGroup)
			cmb.simplex = self
			self.combos.append(cmb)

		for x in itertools.chain(self.sliders, self.combos):
			x.prog.name = x.name

	def loadJSON(self, jsString):
		''' Convenience method to load a JSON string definition '''
		self.loadDefinition(json.loads(jsString))

	def getRestName(self):
		''' Unified rest object name creation '''
		return "Rest_{0}".format(self.name)

	def dump(self):
		''' Dump the definition dictionary to a json string '''
		return json.dumps(self.buildDefinition())

	def exportAbc(self, path, pBar=None):
		''' Export the current mesh to a file '''
		self.extractExternal(path, self.DCC.mesh, pBar)

	def extractExternal(self, path, dccMesh, pBar=None):
		''' Extract shapes from an arbitrary mesh based on the current simplex '''
		defDict = self.buildDefinition()
		jsString = json.dumps(defDict)

		arch = OArchive(str(path)) # alembic does not like unicode filepaths
		try:
			par = OXform(arch.getTop(), str(self.name))
			props = par.getSchema().getUserProperties()
			prop = OStringProperty(props, "simplex")
			prop.setValue(str(jsString))
			abcMesh = OPolyMesh(par, str(self.name))
			self.DCC.exportAbc(dccMesh, abcMesh, defDict, pBar)

		finally:
			del arch

	def _buildRestName(self):
		''' Customize the restshape name '''
		return "Rest_{0}".format(self.name)

	def setSlidersWeights(self, sliders, weights):
		''' Set the weights of multiple sliders as one action '''
		for slider, weight in zip(sliders, weights):
			slider.value = weight
		self.DCC.setSlidersWeights(sliders, weights)
		if self.sliderModel:
			for slider in sliders:
				index = self.sliderModel.indexFromItem(slider, 1)
				self.sliderModel.dataChanged.emit(index, index)



# Hierarchy Helpers
def coerceToType(items, typ, tree):
	''' Get a list of indices of a specific role based on a given index list
	Lists containing parents of the role fall down to their children
	Lists containing children of the role climb up to their parents
	'''
	targetDepth = typ.classDepth

	children = []
	parents = []
	out = []
	for item in items:
		depth = item.classDepth
		if depth < targetDepth:
			parents.append(item)
		elif depth > targetDepth:
			children.append(item)
		else:
			out.append(item)

	out.extend(coerceToChildType(parents, typ, tree))
	out.extend(coerceToParentType(children, typ, tree))
	out = list(set(out))
	return out

def coerceToChildType(items, typ, tree):
	''' Get a list of indices of a specific role based on a given index list
	Lists containing parents of the role fall down to their children
	'''
	targetDepth = typ.classDepth
	out = []

	for item in items:
		depth = item.classDepth
		if depth < targetDepth:
			# Too high up, grab children
			queue = [item]
			depthItems = []
			while queue:
				check = queue.pop()
				if check.depth < targetDepth:
					for row in check.rowCount(tree):
						queue.append(check.child(tree, row))
				else:
					depthItems.append(item)
			# I'm Paranoid
			depthItems = [i for i in depthItems if i.classDepth == targetDepth]
			out.extend(depthItems)
		elif depth == targetDepth:
			out.append(item)

	out = list(set(out))
	return out

def coerceToParentType(items, typ, tree):
	''' Get a list of indices of a specific role based on a given index list
	Lists containing children of the role climb up to their parents
	'''
	targetDepth = typ.classDepth
	out = []
	for item in items:
		depth = item.classDepth
		if depth > targetDepth:
			ii = item
			while ii.classDepth > targetDepth:
				ii = ii.parent(tree)
			if ii.classDepth == targetDepth:
				out.append(ii)
		elif depth == targetDepth:
			out.append(item)

	out = list(set(out))
	return out

def coerceToRoots(items, tree):
	''' Get the topmost items for each brach in the hierarchy '''
	items = sorted(items, key=lambda x: x.classDepth, reverse=True)
	# Check each item to see if any of it's ancestors
	# are in the selection list.  If not, it's a root
	roots = []
	for item in items:
		par = item.parent(tree)
		while par is not None:
			if par in items:
				break
			par = par.parent(tree)
		else:
			roots.append(item)
	return roots



# BASE MODELS
class SimplexModel(QAbstractItemModel):
	def __init__(self, simplex, parent):
		super(SimplexModel, self).__init__(parent)
		self.simplex = simplex

	def index(self, row, column, parIndex):
		if not parIndex.isValid():
			return self.createIndex(row, column, self.simplex)
		par = parIndex.internalPointer()
		child = self.getChildItem(par, row)
		if isinstance(child, QModelIndex):
			return child
		return self.createIndex(row, column, child)

	def parent(self, index):
		if not index.isValid():
			return QModelIndex()
		item = index.internalPointer()
		if item is None:
			return QModelIndex()
		par = self.getParentItem(item)
		#if par is None:
			#return QModelIndex()
		row = self.getItemRow(par)
		if row is None:
			return QModelIndex()
		return self.createIndex(row, 0, par)

	def rowCount(self, parent):
		if not parent.isValid():
			return 1
		obj = parent.internalPointer()
		ret = self.getItemRowCount(obj)
		return ret

	def columnCount(self, parent):
		return 3

	#def hasChildren(self, parent):
		#return bool(self.rowCount(parent))

	def data(self, index, role):
		if not index.isValid():
			return None
		item = index.internalPointer()
		return self.getItemData(item, index.column(), role)

	#def setData(self, index, value, role):
		#pass
		#self.dataChanged.emit(index, index, role)

	def flags(self, index):
		if not index.isValid():
			return Qt.ItemIsEnabled
		return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable

	def headerData(self, section, orientation, role):
		if orientation == Qt.Horizontal:
			if role == Qt.DisplayRole:
				sects = ("Items", "Slide", "Value")
				return sects[section]
		return None

	def indexFromItem(self, item, column=0):
		row = self.getItemRow(item)
		return self.createIndex(row, column, item)

	def itemFromIndex(self, index):
		return index.internalPointer()

	def updateTickValues(self, updatePairs):
		''' Update all the drag-tick values at once. This should be called
		by a single-shot timer or some other once-per-refresh mechanism
		'''
		# Don't make this mouse-tick be stackable. That way
		# we don't update the whole System for a slider value changes
		sliderList = []
		progs = []
		comboList = []
		for i in updatePairs:
			if isinstance(i[0], Slider):
				sliderList.append(i)
			elif isinstance(i[0], ProgPair):
				progs.append(i)
			elif isinstance(i[0], ComboPair):
				comboList.append(i)

		if progs:
			progPairs, values = zip(*progs)
			self.simplex.setShapesValues(progPairs, values)

		if sliderList:
			sliders, values = zip(*sliderList)
			self.simplex.setSlidersWeights(sliders, values)

		if comboList:
			comboPairs, values = zip(*comboList)
			self.simplex.setCombosValues(comboPairs, values)


class SliderModel(SimplexModel):
	def getChildItem(self, parent, row):
		try:
			if isinstance(parent, Simplex):
				return parent.sliderGroups[row]
			elif isinstance(parent, Group):
				return parent.items[row]
			elif isinstance(parent, Slider):
				return parent.prog.pairs[row]
			elif isinstance(parent, ProgPair):
				return None
		except IndexError:
			pass
		return QModelIndex()

	def getItemRow(self, item):
		row = None
		if isinstance(item, Group):
			row = item.simplex.sliderGroups.index(item)
		elif isinstance(item, Slider):
			row = item.group.items.index(item)
		elif isinstance(item, ProgPair):
			row = item.prog.pairs.index(item)
		elif isinstance(item, Simplex):
			row = 0
		return row

	def getParentItem(self, item):
		if isinstance(item, Group):
			par = item.simplex
		elif isinstance(item, Slider):
			par = item.group
		elif isinstance(item, ProgPair):
			par = item.prog.controller
		else:
			par = None
		return par

	def getItemRowCount(self, item):
		if isinstance(item, Simplex):
			return len(item.sliderGroups)
		elif isinstance(item, Group):
			return len(item.items)
		elif isinstance(item, Slider):
			return len(item.prog.pairs)
		return 0

	def getItemData(self, item, column, role):
		if role in (Qt.DisplayRole, Qt.EditRole):
			if column == 0:
				if isinstance(item, (Simplex, Group, Slider, ProgPair)):
					return item.name
			elif column == 1:
				if isinstance(item, Slider):
					return item.value
			elif column == 2:
				if isinstance(item, ProgPair):
					return item.value
		return None


class ComboModel(SimplexModel):
	def getChildItem(self, item, row):
		try:
			if isinstance(item, Simplex):
				return item.comboGroups[row]
			elif isinstance(item, Group):
				return item.items[row]
			elif isinstance(item, Combo):
				if row == len(item.pairs):
					return item.prog
				return item.pairs[row]
			elif isinstance(item, ComboPair):
				return None
			elif isinstance(item, Progression):
				return item.pairs[row]
			elif isinstance(item, ProgPair):
				return None
		except IndexError:
			pass
		return QModelIndex()

	def getItemRow(self, item):
		row = None
		if isinstance(item, Group):
			row = item.simplex.comboGroups.index(item)
		elif isinstance(item, Combo):
			row = item.group.items.index(item)
		elif isinstance(item, ComboPair):
			row = item.combo.pairs.index(item)
		elif isinstance(item, Progression):
			row = len(item.controller.pairs)
		elif isinstance(item, ProgPair):
			row = item.prog.pairs.index(item)
		elif isinstance(item, Simplex):
			row = 0
		return row

	def getParentItem(self, item):
		if isinstance(item, Group):
			par = item.simplex
		elif isinstance(item, Combo):
			par = item.group
		elif isinstance(item, ComboPair):
			par = item.combo
		elif isinstance(item, Progression):
			par = item.controller
		elif isinstance(item, ProgPair):
			par = item.prog
		else:
			par = None
		return par

	def getItemRowCount(self, item):
		if isinstance(item, Simplex):
			return len(item.comboGroups)
		elif isinstance(item, Group):
			return len(item.items)
		elif isinstance(item, Combo):
			return len(item.pairs) + 1
		elif isinstance(item, Progression):
			return len(item.pairs)
		return 0

	def getItemData(self, item, column, role):
		if role in (Qt.DisplayRole, Qt.EditRole):
			if column == 0:
				if isinstance(item, (Simplex, Group, Combo, ComboPair, ProgPair)):
					return item.name
				elif isinstance(item, Progression):
					return "SHAPES"
			elif column == 1:
				if isinstance(item, ComboPair):
					return item.value
			elif column == 2:
				if isinstance(item, ProgPair):
					return item.value
		return None



# FILTER MODELS
class SimplexFilterModel(QSortFilterProxyModel):
	def __init__(self, parent=None):
		super(SimplexFilterModel, self).__init__(parent)
		self.filterString = ""
		self.isolateList = []

	def indexFromItem(self, item):
		sourceModel = self.sourceModel()
		sourceIndex = sourceModel.indexFromItem(item)
		return self.mapFromSource(sourceIndex)

	def itemFromIndex(self, index):
		sourceModel = self.sourceModel()
		return sourceModel.itemFromIndex(index)

	def filterAcceptsRow(self, sourceRow, sourceParent):
		column = 0 #always sort by the first column #column = self.filterKeyColumn()
		sourceIndex = self.sourceModel().index(sourceRow, column, sourceParent)
		if sourceIndex.isValid():
			if self.filterString or self.isolateList:
				sourceItem = self.sourceModel().itemFromIndex(sourceIndex)
				if isinstance(sourceItem, (ProgPair, Slider, Combo)):
					if not self.checkChildren(sourceItem):
						return False

		return super(SimplexFilterModel, self).filterAcceptsRow(sourceRow, sourceParent)

	def checkChildren(self, sourceItem):
		# Recursively check the children of this object.
		# If any child matches the filter, then this object should be shown
		itemstring = sourceitem.name
		if self.isolateList:
			if itemString in self.isolateList:
				if self.filterString:
					if fnmatchcase(itemString, "*{0}*".format(self.filterString)):
						return True
				else:
					return True
		elif fnmatchcase(itemString, "*{0}*".format(self.filterString)):
			return True

		if sourceItem.hasChildren():
			for row in xrange(sourceItem.rowCount()):
				if self.checkChildren(sourceItem.child(row, 0)):
					return True
		return False


class ComboFilterModel(SimplexFilterModel):
	""" Filter by slider when Show Dependent Combos is checked """
	def __init__(self, parent=None):
		super(ComboFilterModel, self).__init__(parent)
		self.requires = []
		self.filterRequiresAll = False
		self.filterRequiresAny = False
		self.filterRequiresOnly = False

		self.filterShapes = True

	def filterAcceptsRow(self, sourceRow, sourceParent):
		column = 0 #always sort by the first column #column = self.filterKeyColumn()
		sourceIndex = self.sourceModel().index(sourceRow, column, sourceParent)
		if sourceIndex.isValid():
			data = self.sourceModel().itemFromIndex(sourceIndex)
			if self.filterShapes:
				# ignore the SHAPE par if there's nothing under there
				if isinstance(data, Progression):
					if len(data.pairs) <= 2:
						return False
				# Ignore shape things if requested
				if isinstance(data, ProgPair):
					if len(data.prog.pairs) <= 2:
						return False
					elif data.shape.isRest:
						return False
			if (self.filterRequiresAny or self.filterRequiresAll or self.filterRequiresOnly) and self.requires:
				# Ignore items that don't use the required sliders if requested
				if isinstance(data, Combo):
					sliders = [i.slider for i in data.pairs]
					if self.filterRequiresAll:
						if not all(r in sliders for r in self.requires):
							return False
					elif self.filterRequiresAny:
						if not any(r in sliders for r in self.requires):
							return False
					elif self.filterRequiresOnly:
						if not all(r in self.requires for r in sliders):
							return False

		return super(ComboFilterModel, self).filterAcceptsRow(sourceRow, sourceParent)


class SliderFilterModel(SimplexFilterModel):
	""" Hide single shapes under a slider """
	def __init__(self, parent=None):
		super(SliderFilterModel, self).__init__(parent)
		self.doFilter = True

	def filterAcceptsRow(self, sourceRow, sourceParent):
		column = 0 #always sort by the first column #column = self.filterKeyColumn()
		sourceIndex = self.sourceModel().index(sourceRow, column, sourceParent)
		if sourceIndex.isValid():
			if self.doFilter:
				data = self.sourceModel().itemFromIndex(sourceIndex)
				if isinstance(data, ProgPair):
					if len(data.prog.pairs) <= 2:
						return False
					elif data.shape.isRest:
						return False

		return super(SliderFilterModel, self).filterAcceptsRow(sourceRow, sourceParent)



