import torch
import torch.nn as nn

from typing import Optional
from dataclasses import dataclass, replace

from torchlensmaker.raytracing import (
    refraction,
    reflection,
    ray_point_squared_distance,
    position_on_ray,
    rays_to_coefficients,
    rot2d,
)

from torchlensmaker.torch_extensions import OpticalSequence

from torchlensmaker.surface import Surface
from torchlensmaker.shapes import Line


def loss_nonpositive(parameters, scale=1):
    return torch.where(parameters > 0, torch.pow(scale*parameters, 2), torch.zeros_like(parameters))


@dataclass
class OpticalData:
    """
    Holder class for the data that's passed between optical elements
    """

    # Tensor of shape (N, 2)
    # Rays origins points
    rays_origins: torch.Tensor

    # Tensor of shape (N, 2)
    # Rays unit vectors
    rays_vectors: torch.Tensor

    # Tensor of shape (2,)
    # Position of the next optical element
    target: torch.Tensor

    # None or Tensor of shape (N,)
    # Mask array indicating which rays from the previous data in the optical
    # stack were blocked by the previous optical element
    # "block" includes hitting an absorbing surface but also not hitting anything
    blocked: Optional[torch.Tensor]

    # experimental
    # coordinates normalized to (-1, 1) of sample points in 'number of rays' space
    coord_base: torch.Tensor

    # experimental
    # coordinates normalied to (-1, 1) of sample points in 'object' space
    coord_object: torch.Tensor

    # Tensor of one element
    # Loss accumulator
    loss: torch.Tensor


default_input = OpticalData(
    rays_origins = torch.empty((0, 2)),
    rays_vectors = torch.empty((0, 2)),
    target = torch.zeros(2),
    blocked = None,
    coord_base = torch.empty((0,)),
    coord_object = torch.empty((0,)),
    loss = torch.tensor(0.),
)


class FocalPoint(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, inputs: OpticalData, sampling: dict):
        num_rays = inputs.rays_origins.shape[0]
        sum_squared = ray_point_squared_distance(inputs.rays_origins, inputs.rays_vectors, inputs.target).sum()
        loss = sum_squared / num_rays

        return replace(inputs, loss=inputs.loss + loss)


class Image(nn.Module):
    "An image is a set of focal points that map to the observed object"

    def __init__(self, height):
        super().__init__()
        self.height = torch.as_tensor(height, dtype=torch.float32)
    
    def forward(self, inputs: OpticalData, sampling: dict):
        # Compute image loss

        # First, make the 2D points that correspond to the object sampling
        
        points_y = inputs.coord_object * self.height - self.height / 2
        points_x = inputs.target[0].expand_as(points_y)

        points = torch.stack((points_x, points_y), dim=-1)

        num_rays = inputs.rays_origins.shape[0]
        sum_squared = ray_point_squared_distance(inputs.rays_origins, inputs.rays_vectors, points).sum()
        loss = sum_squared / num_rays

        return replace(inputs, loss=inputs.loss + loss)
    
    def loss(self, inputs: OpticalData, _: dict):
        # target points from rays origin coordinates
        pass
        #points = s...

        #return ray_point_squared_distance(inputs.rays_origins, inputs.ray_vectors, points).sum()


class PointSource(nn.Module):
    def __init__(self, beam_angle, height=0, object_coord=0.):
        """
        height: height of the point source above the principal axis
        beam_angle: total angle of the emitted beam of rays (in degrees)
        """

        super().__init__()
        self.beam_angle = torch.deg2rad(
            torch.as_tensor(beam_angle, dtype=torch.float32)
        )
        self.height = torch.as_tensor(height, dtype=torch.float32)
        self.object_coord = torch.as_tensor(object_coord, dtype=torch.float32)

    def forward(self, inputs: OpticalData, sampling: dict):

        num_rays = sampling["rays"]

        # Create new rays by sampling the beam angle
        rays_origins = torch.tile(
            inputs.target + torch.tensor([0.0, self.height]), (num_rays, 1)
        )

        angles = torch.linspace(
            -self.beam_angle / 2, self.beam_angle / 2, num_rays
        )
        rays_vectors = rot2d(torch.tensor([1.0, 0.0]), angles)

        # normalized coordinate along the base dimension
        coord_base = (angles + self.beam_angle / 2) / self.beam_angle
        coord_object = self.object_coord.expand_as(coord_base)

        # Add new rays to the input rays
        return OpticalData(
            torch.cat((inputs.rays_origins, rays_origins), dim=0),
            torch.cat((inputs.rays_vectors, rays_vectors), dim=0),
            inputs.target,
            None,
            torch.cat((inputs.coord_base, coord_base)),
            torch.cat((inputs.coord_object, coord_object)),
            inputs.loss,
        )


class PointSourceAtInfinity(nn.Module):
    def __init__(self, beam_diameter, angle=0., object_coord=0.):
        """
        beam_diameter: diameter of the beam of parallel light rays
        angle: angle of indidence with respect to the principal axis, in degrees

        samples along the base sampling dimension
        """

        super().__init__()
        self.beam_diameter = torch.as_tensor(beam_diameter, dtype=torch.float32)
        self.angle = torch.deg2rad(torch.as_tensor(angle, dtype=torch.float32))
        self.object_coord = torch.as_tensor(object_coord, dtype=torch.float32)

    def forward(self, inputs: OpticalData, sampling: dict):
        # Create new rays by sampling the beam diameter
        num_rays = sampling["rays"]
        margin = 0.1  # TODO
        rays_x = torch.zeros(num_rays)
        rays_y = torch.linspace(
            -self.beam_diameter / 2 + margin,
            self.beam_diameter / 2 - margin,
            num_rays,
        )

        rays_origins = inputs.target + torch.column_stack((rays_x, rays_y))
        vect = rot2d(torch.tensor([1.0, 0.0]), self.angle)
        rays_vectors = torch.tile(vect, (num_rays, 1))

        # normalized coordinate along the base dimension
        coord_base = (rays_y + self.beam_diameter / 2) / self.beam_diameter

        coord_object = self.object_coord.expand_as(coord_base)

        # Add new rays to the input rays
        return OpticalData(
            torch.cat((inputs.rays_origins, rays_origins), dim=0),
            torch.cat((inputs.rays_vectors, rays_vectors), dim=0),
            inputs.target,
            None,
            torch.cat((inputs.coord_base, coord_base)),
            torch.cat((inputs.coord_object, coord_object)),
            inputs.loss,
        )


class ObjectAtInfinity(nn.Module):
    def __init__(self, beam_diameter, angular_size, angle=0):
        """
        angular_size: apparent angular size of the object, in degrees
        angle: angle of incidence of the object's center with the principal axis, in degrees
        """

        super().__init__()
        self.beam_diameter = torch.as_tensor(beam_diameter, dtype=torch.float32)
        self.angular_size = torch.as_tensor(angular_size, dtype=torch.float32)
        self.angle = torch.deg2rad(torch.as_tensor(angle, dtype=torch.float32))

    def forward(self, inputs: OpticalData, sampling: dict):
        # An object at infinity is a collection of points at infinity,
        # sampled along the object's angular size

        num_samples = sampling["object"]

        angles = torch.linspace(-self.angular_size/2., self.angular_size/2, num_samples)

        modules = OpticalSequence()

        for angle in angles:
            # normalized parametric coordinate on the object
            # (-1, 1) or (0, 1) ?
            # t = ...
            # add a PointSourceAtInfinity with that t value
            current_angle = angle
            object_coord = (angle + self.angular_size / 2) / self.angular_size
            mod = PointSourceAtInfinity(self.beam_diameter, angle=current_angle + self.angle, object_coord=object_coord)
            modules.append(mod)

        return modules.forward(inputs, sampling)


class Gap(nn.Module):
    def __init__(self, offset):
        super().__init__()
        self.offset = offset
    
    def forward(self, inputs: OpticalData, sampling: dict):
        offset = torch.stack((torch.as_tensor(self.offset), torch.tensor(0.)))
        new_target = inputs.target + offset

        return OpticalData(inputs.rays_origins, inputs.rays_vectors, new_target, None, inputs.coord_base, inputs.coord_object, inputs.loss)


class Aperture(nn.Module):
    def __init__(self, height, diameter):
        super().__init__()
        self.height = height
        self.diameter = diameter
        self.shape = Line(diameter)
    
    def forward(self, inputs: OpticalData, sampling: dict):
        surface = Surface(self.shape, pos=inputs.target)

        # TODO factor common collision code with OpticalSurface
        # For all rays, find the intersection with the surface and the normal vector at the intersection
        lines = rays_to_coefficients(inputs.rays_origins, inputs.rays_vectors)
        sols = surface.collide(lines)

        # Detect solutions outside the surface domain
        valid = torch.logical_and(sols <= surface.domain()[1], sols >= surface.domain()[0])
        
        # Filter data to keep only colliding rays
        sols = sols[valid]
        rays_origins = inputs.rays_origins[valid]
        rays_vectors = inputs.rays_vectors[valid]
        blocked = ~valid

        collision_points = surface.evaluate(sols)

        # TODO
        coord_base_filtered = inputs.coord_base[valid]
        coord_object_filtered = inputs.coord_object[valid]

        return OpticalData(collision_points, rays_vectors, inputs.target, blocked, coord_base_filtered, coord_object_filtered, inputs.loss)


class OpticalSurface(nn.Module):
    """
    Common base class for ReflectiveSurface and RefractiveSurface
    """

    def __init__(self, shape, scale=1., anchors=("origin", "origin")):
        super().__init__()

        self.shape = shape
        self.scale = scale
        self.anchors = anchors

    def surface(self, pos):
        return Surface(self.shape, pos=pos, scale=self.scale, anchor=self.anchors[0])
    
    def forward(self, inputs: OpticalData, sampling: dict):
        surface = self.surface(inputs.target)
        valid = None

        # special case for zero rays, TODO remove this and make sure the inner code works with B=0
        if inputs.rays_origins.numel() == 0:
            collision_points = torch.empty((0, 0))
            output_rays = torch.empty((0, 0))
            blocked = None
        else:
            # For all rays, find the intersection with the surface and the normal vector at the intersection
            lines = rays_to_coefficients(inputs.rays_origins, inputs.rays_vectors)
            sols = surface.collide(lines)

            # Detect solutions outside the surface domain
            valid = torch.logical_and(sols <= surface.domain()[1], sols >= surface.domain()[0])
            if False and torch.sum(~valid) > 0:
                raise RuntimeError("Some rays do not collide with the surface")

            # Filter data to keep only colliding rays
            sols = sols[valid]
            rays_origins = inputs.rays_origins[valid]
            rays_vectors = inputs.rays_vectors[valid]
            blocked = ~valid

            # Evaluate collision points and normals
            collision_points, surface_normals = surface.evaluate(sols), surface.normal(sols)

            # Verify no weirdness in the data
            assert torch.all(torch.isfinite(collision_points))
            assert torch.all(torch.isfinite(surface_normals))

            # Make sure collisions are in front of rays
            if False:
                ts = position_on_ray(rays_origins, rays_vectors, collision_points)
                if torch.any(ts <= 0):
                    print("warning: some ts <=0")
                if not torch.all(ts > 0): # TODO regression term on ts < 0 (== lens surface collision)
                    print("!! Some ts <= 0")
                    raise RuntimeError("negative collisions")
            
            # A surface always has two opposite normals, so keep the one pointing against the ray
            # i.e. the normal such that dot(normal, ray) < 0
            dot = torch.sum(surface_normals * rays_vectors, dim=1)
            collision_normals = torch.where((dot > 0).unsqueeze(1).expand(-1, 2), -surface_normals, surface_normals)

            # Verify no weirdness again
            assert torch.all(torch.isfinite(collision_normals))
            
            # Refract or reflect rays based on the derived class implementation
            output_rays = self.optical_function(rays_vectors, collision_normals)

        new_target = surface.at(self.anchors[1])

        # TODO
        if valid is not None:
            coord_base_filtered = inputs.coord_base[valid]
            coord_object_filtered = inputs.coord_object[valid]
        else:
            coord_base_filtered = inputs.coord_base
            coord_object_filtered = inputs.coord_object

        return OpticalData(collision_points, output_rays, new_target, blocked, coord_base_filtered, coord_object_filtered, inputs.loss)


class ReflectiveSurface(OpticalSurface):
    def __init__(self, shape, scale=1., anchors=("origin", "origin")):
        super().__init__(shape, scale, anchors)
        

    def optical_function(self, rays, normals):
        return reflection(rays, normals)


class RefractiveSurface(OpticalSurface):
    def __init__(self, shape, n, scale=1., anchors=("origin", "origin")):
        super().__init__(shape, scale, anchors)
        self.n1, self.n2 = n
        
    def optical_function(self, rays, normals):
        return refraction(rays, normals, self.n1, self.n2, critical_angle='clamp')
